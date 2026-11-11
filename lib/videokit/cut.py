"""One recording of a whole script, cut into its lines.

Most videos get their lines spoken one by one. When a video's voice is the recording that came
with its design (tools/audio/voiceover_tts.py, when no voice can be saved), the whole script
arrives as ONE piece of audio, and every later step still wants a file per line: `kit voice`
tightens and times each line, and each take is generated from its own line.

The lines are found the way `kit voice` finds words: the transcriber supplies the clock, the
script supplies the words. A clock is not exact at a pause (a word after a pause is often said
to start where the word before it ended), so each cut is then moved into the quiet the
recording really has between the two lines: the longest pause between the middle of one line's
last word and the middle of the next line's first word. No cut touches a word.

A recording that does not say the script (a line missing, words added, no pause to cut in) is
refused with the reason; the caller tries another recording or another voice.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from array import array
from pathlib import Path
from typing import Any

from lib.videokit.voice import Transcribe, conform_edges, norm, script_tokens, whisper_words

RATE = 44100
STEP = RATE // 100  # the recording's loudness is read every 10 ms
# The take tool's pause filter calls -40 dB silence (TRIM_FILTER in tools/avatar/fal_avatar.py);
# a cut made in anything quieter lies where `kit voice` hears silence too.
QUIET_DB = -40.0
MIN_PAUSE = 0.05
# What a line keeps of the pause before and after it; `kit voice` tightens both again.
LEAD, TAIL = 0.06, 0.20
FADE_IN, FADE_OUT = 0.005, 0.015
# Below these the recording does not say the script (the whole, or one line of it).
MIN_HEARD, MIN_LINE_HEARD = 0.7, 0.5
# A line may be heard this much longer than it is written before it counts as saying more.
MOST_EXTRA, EXTRA_CHARACTERS = 1.35, 12
MP3_BITRATE = "192k"


class CutError(Exception):
    """This recording cannot be cut into the lines; its words say why."""


def _samples(recording: Path) -> array:
    try:
        done = subprocess.run(["ffmpeg", "-v", "error", "-i", str(recording), "-vn", "-ac", "1", "-ar", str(RATE),
                               "-f", "s16le", "-"], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CutError(f"the recording could not be read ({type(exc).__name__})") from None
    if done.returncode != 0 or len(done.stdout) < 2 * STEP:
        raise CutError("the recording could not be read")
    pcm = array("h")
    pcm.frombytes(done.stdout[:len(done.stdout) // 2 * 2])
    if sys.byteorder == "big":
        pcm.byteswap()
    return pcm


def _levels(pcm: array) -> list[float]:
    """How loud every 10 ms of the recording is, in dB below full scale."""
    levels = []
    for start in range(0, len(pcm) - STEP + 1, STEP):
        power = sum(v * v for v in pcm[start:start + STEP]) / STEP
        levels.append(10 * math.log10(power / 32768.0 ** 2) if power > 0 else -120.0)
    return levels


def _pauses(levels: list[float], lo: float, hi: float) -> list[tuple[float, float]]:
    """Every stretch of quiet that lies between two times (seconds), as (start, end)."""
    first = max(0, int(round(lo * 100)))
    last = min(len(levels), int(round(hi * 100)))
    found, begun = [], None
    for index in range(first, last):
        quiet = levels[index] < QUIET_DB
        if quiet and begun is None:
            begun = index
        elif not quiet and begun is not None:
            found.append((begun / 100, index / 100))
            begun = None
    if begun is not None:
        found.append((begun / 100, last / 100))
    return [(a, b) for a, b in found if b - a >= MIN_PAUSE - 1e-9]


def _whole(levels: list[float], pause: tuple[float, float]) -> tuple[float, float]:
    """A pause as long as it really is: the stretch it was looked for in may have cut it short."""
    first, last = int(round(pause[0] * 100)), int(round(pause[1] * 100))
    while first > 0 and levels[first - 1] < QUIET_DB:
        first -= 1
    while last < len(levels) and levels[last] < QUIET_DB:
        last += 1
    return first / 100, last / 100


def _speech_edges(levels: list[float]) -> tuple[float, float]:
    """Where the recording's sound begins and where it ends."""
    loud = [index for index, level in enumerate(levels) if level >= QUIET_DB]
    if not loud:
        raise CutError("the recording is silent")
    return loud[0] / 100, (loud[-1] + 1) / 100


def _encode(pcm: array, target: Path) -> None:
    fade_in, fade_out = min(len(pcm), int(FADE_IN * RATE)), min(len(pcm), int(FADE_OUT * RATE))
    for k in range(fade_in):
        pcm[k] = int(pcm[k] * k / fade_in)
    for k in range(fade_out):
        pcm[-1 - k] = int(pcm[-1 - k] * k / fade_out)
    if sys.byteorder == "big":
        pcm = array("h", pcm)
        pcm.byteswap()
    target.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", "pipe:0",
                           "-c:a", "libmp3lame", "-b:a", MP3_BITRATE, "-f", "mp3", str(target)],
                          input=pcm.tobytes(), capture_output=True, timeout=120)
    if done.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        raise CutError("a line could not be written")


def find_lines(levels: list[float], total: float, lines: list[dict[str, str]],
               heard: list[dict[str, Any]]) -> dict[str, Any]:
    """Where each line lies in the recording. Raises CutError when the recording does not say
    the script or has no pause between two lines."""
    tokens: list[str] = []
    owner: list[int] = []
    for index, line in enumerate(lines):
        mine = script_tokens(str(line["text"]))
        if not mine:
            raise CutError(f"line {line['id']} has no words")
        tokens.extend(mine)
        owner.extend([index] * len(mine))
    if not heard:
        raise CutError("no words were heard in the recording")
    words, _share, edges = conform_edges(tokens, heard, total)
    score = [(start + end) / 2 for start, end in edges]
    spans = []  # per line: its first and last token, and how much of it was heard
    for index, line in enumerate(lines):
        mine = [k for k, who in enumerate(owner) if who == index]
        share = sum(score[k] for k in mine) / len(mine)
        if share < MIN_LINE_HEARD:
            raise CutError(f"line {line['id']} was not heard in the recording ({share:.0%} of its words)")
        spans.append((mine[0], mine[-1], share))
    whole = sum(score) / len(score)
    if whole < MIN_HEARD:
        raise CutError(f"only {whole:.0%} of the script's words were heard in the recording")

    begin, end = _speech_edges(levels)
    pauses: list[tuple[float, float]] = []
    for index in range(len(lines) - 1):
        last, first = spans[index][1], spans[index + 1][0]
        if not (edges[last][1] or edges[first][0]):
            raise CutError(f"the end of line {lines[index]['id']} and the start of line {lines[index + 1]['id']} "
                           "were not heard clearly")
        lo = (words[last]["start"] + words[last]["end"]) / 2
        hi = (words[first]["start"] + words[first]["end"]) / 2
        between = _pauses(levels, lo, hi)
        if not between:
            raise CutError(f"there is no pause between line {lines[index]['id']} and line {lines[index + 1]['id']}")
        pauses.append(_whole(levels, max(between, key=lambda pause: pause[1] - pause[0])))
    for before, after in zip(pauses, pauses[1:]):
        if after[0] < before[1]:
            raise CutError("the lines were not found in their order")

    found = []
    for index, line in enumerate(lines):
        starts = begin if index == 0 else pauses[index - 1][1]
        stops = end if index == len(lines) - 1 else pauses[index][0]
        lead = LEAD if index == 0 else min(LEAD, (pauses[index - 1][1] - pauses[index - 1][0]) / 2)
        tail = TAIL if index == len(lines) - 1 else min(TAIL, (pauses[index][1] - pauses[index][0]) / 2)
        a, b = max(0.0, starts - lead), min(total, stops + tail)
        if b - a < 0.1:
            raise CutError(f"line {line['id']} has no length in the recording")
        # Does the stretch say more than the line? Counted in letters, so any script works. A
        # heard word belongs to the line it ends in: the clock may start a word in the pause
        # before it, never end it there.
        written = sum(len(norm(tokens[k])) for k in range(spans[index][0], spans[index][1] + 1))
        said = sum(len(norm(str(h["word"]))) for h in heard
                   if (index == 0 or h["end"] >= pauses[index - 1][1])
                   and (index == len(lines) - 1 or h["end"] < pauses[index][1]))
        if said > written * MOST_EXTRA and said - written > EXTRA_CHARACTERS:
            raise CutError(f"line {line['id']} is said with words the script does not have")
        found.append({"id": str(line["id"]), "start": round(a, 3), "end": round(b, 3),
                      "seconds": round(b - a, 3), "heard": round(spans[index][2], 3)})
    return {"seconds": round(total, 3), "heard": round(whole, 3), "lines": found,
            "pauses": [{"after": str(lines[index]["id"]), "start": round(a, 3), "end": round(b, 3),
                        "seconds": round(b - a, 3)} for index, (a, b) in enumerate(pauses)]}


def cut_lines(recording: Path, lines: list[dict[str, str]], *, transcribe: Transcribe | None = None,
              language: str | None = None, model_size: str = "base", work_dir: Path | None = None) -> dict[str, Any]:
    """Cut one recording of the whole script into one MP3 per line.

    `lines` are {"id", "text", "output_path"} in the order they are spoken; a line without an
    output path is found and left out (a short script said twice). Every file is written, or
    none is: a recording that is refused (CutError) has changed nothing. Returns where each
    line was found."""
    if not lines:
        raise CutError("there are no lines to cut")
    recording = Path(recording)
    pcm = _samples(recording)
    total = len(pcm) / RATE
    run = transcribe or whisper_words(model_size, language, work_dir)
    try:
        heard = run(recording)
    except CutError:
        raise
    except Exception as exc:  # the transcriber's own failure is this recording's refusal
        raise CutError(f"the recording's words could not be timed ({exc})") from None
    report = find_lines(_levels(pcm), total, lines, heard)
    parts = {}
    try:
        for line, place in zip(lines, report["lines"]):
            if not line.get("output_path"):
                continue
            target = Path(line["output_path"])
            part = target.with_name(f".{target.stem}.part.mp3")
            parts[target] = part
            _encode(pcm[int(place["start"] * RATE):int(place["end"] * RATE)], part)
        for target, part in parts.items():
            os.replace(part, target)
    finally:
        for part in parts.values():
            part.unlink(missing_ok=True)
    return report
