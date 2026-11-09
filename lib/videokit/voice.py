"""Preparing the voice: trimmed line audio, word timings pinned to the script, the timing spine.

Run right after the lines are spoken (TTS), before any take exists. For every line it

- tightens the audio with the take tool's own pause filter, so a take generated from the
  prepared file carries exactly this audio and needs no trimming of its own;
- times every word of the SCRIPT (not of what a transcriber thought it heard): the
  transcriber supplies the clock, the script supplies the words;
- splits a line that is longer than one take can hold at a sentence pause;
- lays the lines end to end: the spine every cut, caption and the soundtrack hang on.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import math
import re
import subprocess
import unicodedata
from pathlib import Path
from typing import Any, Callable

TAKE_LIMIT_SECONDS = 14.5
AUDIO_EXTENSIONS = (".wav", ".mp3", ".m4a", ".ogg", ".flac")
_SENTENCE_END = re.compile(r"[.!?…。！？][\"')\]]?$")
_SOFT_END = re.compile(r"[,:;—–、，][\"')\]]?$")

Transcribe = Callable[[Path], list[dict[str, Any]]]
# Scripts written without spaces between words: the engine's isUnspaced, same ranges.
_UNSPACED = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\u0e00-\u0e7f\u1780-\u17ff]")


class VoiceError(Exception):
    """Something the director has to fix: a missing line recording, a line that cannot be split."""


def norm(text: str) -> str:
    """The same folding the engine uses to find words: case, punctuation and hyphens ignored."""
    folded = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in folded if ch.isalnum())


def duration_of(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True, timeout=60).stdout.strip()
    return float(out)


def trim(src: Path, dst: Path) -> Path:
    """The take tool's own pause tightening - one definition, so prepared audio and takes agree."""
    from tools.avatar.fal_avatar import FalAvatar
    dst.parent.mkdir(parents=True, exist_ok=True)
    return FalAvatar._trim_silence(src, dst)


def whisper_words(model_size: str = "base", language: str | None = None, work_dir: Path | None = None) -> Transcribe:
    def run(audio: Path) -> list[dict[str, Any]]:
        from tools.analysis.transcriber import Transcriber
        inputs: dict[str, Any] = {"input_path": str(audio), "model_size": model_size}
        if language:
            inputs["language"] = language
        if work_dir is not None:
            inputs["output_dir"] = str(work_dir)
        result = Transcriber().execute(inputs)
        if not result.success:
            raise VoiceError(f"could not time the words of {audio.name}: {result.error}")
        return [{"word": str(w.get("word", "")).strip(), "start": float(w["start"]), "end": float(w["end"])}
                for w in result.data.get("word_timestamps") or [] if str(w.get("word", "")).strip()]
    return run


def _segment(text: str) -> list[str]:
    """Words of a script written without spaces, from the engine's own tokenizer (one
    definition for the estimate and for the real timings). Per character when Node is missing."""
    from lib.videokit import KIT_DIR
    program = ("import {tokenize} from " + json.dumps((KIT_DIR / "engine" / "words.ts").as_uri()) + ";"
               "let s='';process.stdin.on('data',(d)=>s+=d).on('end',()=>console.log(JSON.stringify(tokenize(s))));")
    try:
        done = subprocess.run(["node", "--input-type=module", "-e", program], input=text, capture_output=True,
                              text=True, encoding="utf-8", timeout=60, check=True)
        tokens = json.loads(done.stdout)
        if isinstance(tokens, list) and tokens:
            return [str(t) for t in tokens]
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    tokens = []
    for ch in text:
        if ch.isspace():
            continue
        if norm(ch) or not tokens:
            tokens.append(ch)
        else:
            tokens[-1] += ch
    return tokens


def script_tokens(text: str) -> list[str]:
    """The script's words as the captions will show them. A token of punctuation alone
    (a dash, a middle dot) rides with the word before it."""
    if _UNSPACED.search(text):
        return _segment(text)
    tokens: list[str] = []
    for raw in text.split():
        if not norm(raw) and tokens:
            tokens[-1] = f"{tokens[-1]} {raw}"
        else:
            tokens.append(raw)
    return tokens


def conform(tokens: list[str], heard: list[dict[str, Any]], total: float) -> tuple[list[dict[str, Any]], float]:
    """Give every script token a start and an end from what was heard.

    Returns the timed tokens and the share of tokens whose timing was heard directly."""
    if _UNSPACED.search("".join(tokens)):
        return _conform_characters(tokens, heard, total)
    a = [norm(t) for t in tokens]
    b = [norm(h["word"]) for h in heard]
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    direct = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                times[i1 + k] = (heard[j1 + k]["start"], heard[j1 + k]["end"])
                direct += 1
        elif tag == "replace":
            # The same stretch of audio, written differently ("4x" heard as "four times"):
            # spread the script's tokens over it by length.
            t0, t1 = heard[j1]["start"], heard[j2 - 1]["end"]
            weights = [max(1, len(a[i])) for i in range(i1, i2)]
            cursor = t0
            for i, weight in zip(range(i1, i2), weights):
                span = (t1 - t0) * weight / sum(weights)
                times[i] = (cursor, cursor + span)
                cursor += span
    # Tokens nobody heard: share the gap between their timed neighbours.
    i = 0
    while i < len(tokens):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(tokens) and times[j] is None:
            j += 1
        left = times[i - 1][1] if i > 0 else 0.0  # type: ignore[index]
        right = times[j][0] if j < len(tokens) else total  # type: ignore[index]
        weights = [max(1, len(a[k])) for k in range(i, j)]
        cursor = left
        for k, weight in zip(range(i, j), weights):
            span = max(0.0, right - left) * weight / sum(weights)
            times[k] = (cursor, cursor + span)
            cursor += span
        i = j
    words = []
    previous_end = 0.0
    for token, (start, end) in zip(tokens, times):  # type: ignore[misc]
        start = max(previous_end, min(start, total))
        end = max(start, min(end, total))
        words.append({"text": token, "start": round(start, 3), "end": round(end, 3)})
        previous_end = start
    return words, (direct / len(tokens) if tokens else 1.0)


def _conform_characters(tokens: list[str], heard: list[dict[str, Any]], total: float) -> tuple[list[dict[str, Any]], float]:
    """Scripts without spaces: the transcriber cuts words differently than the script does, so
    match character by character and let each script word span the characters it owns."""
    script = [(i, ch) for i, token in enumerate(tokens) for ch in norm(token)]
    clock: list[tuple[str, float, float]] = []
    for h in heard:
        chars = norm(h["word"])
        for k, ch in enumerate(chars):
            span = (h["end"] - h["start"]) / len(chars)
            clock.append((ch, h["start"] + k * span, h["start"] + (k + 1) * span))
    spans: list[list[float] | None] = [None] * len(tokens)
    matched = 0
    for tag, i1, i2, j1, _j2 in difflib.SequenceMatcher(a=[c for _, c in script], b=[c for c, _, _ in clock], autojunk=False).get_opcodes():
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            index = script[i1 + k][0]
            _, start, end = clock[j1 + k]
            spans[index] = [min(spans[index][0], start), max(spans[index][1], end)] if spans[index] else [start, end]  # type: ignore[index]
            matched += 1
    # Words nobody heard share the gap between their timed neighbours.
    i = 0
    while i < len(tokens):
        if spans[i] is not None:
            i += 1
            continue
        j = i
        while j < len(tokens) and spans[j] is None:
            j += 1
        left = spans[i - 1][1] if i > 0 else 0.0  # type: ignore[index]
        right = spans[j][0] if j < len(tokens) else total  # type: ignore[index]
        width = max(0.0, right - left) / (j - i)
        for n in range(i, j):
            spans[n] = [left + (n - i) * width, left + (n - i + 1) * width]
        i = j
    words = []
    for i, token in enumerate(tokens):
        start, end = spans[i]  # type: ignore[misc]
        previous = words[-1]["start"] if words else 0.0
        start = max(previous, min(start, total))
        words.append({"text": token, "start": round(start, 3), "end": round(max(start, min(end, total)), 3)})
    return words, (matched / len(script) if script else 1.0)


def split_points(words: list[dict[str, Any]], total: float, limit: float = TAKE_LIMIT_SECONDS) -> list[float]:
    """Where to cut a line that is too long for one take: at sentence pauses when there are
    any, else at commas, else between words - as few cuts as possible, as even as possible."""
    if total <= limit:
        return []
    candidates: list[tuple[float, int]] = []  # (time, quality)
    for word, following in zip(words, words[1:]):
        cut = (word["end"] + following["start"]) / 2
        quality = 2 if _SENTENCE_END.search(word["text"]) else 1 if _SOFT_END.search(word["text"]) else 0
        candidates.append((cut, quality))
    for parts in range(math.ceil(total / limit), len(words) + 1):
        for floor in (2, 1, 0):
            pool = [c for c in candidates if c[1] >= floor]
            cuts: list[float] = []
            for k in range(1, parts):
                target = total * k / parts
                usable = [c for c, _ in pool if not cuts or c > cuts[-1] + 0.5]
                if not usable:
                    break
                cuts.append(min(usable, key=lambda c: abs(c - target)))
            else:
                edges = [0.0, *cuts, total]
                if all(b - a <= limit for a, b in zip(edges, edges[1:])):
                    return cuts
    raise VoiceError(f"a {total:.1f} s line cannot be split into takes of at most {limit} s; shorten it")


def find_recording(project: Path, line: dict[str, Any]) -> Path:
    if line.get("audio"):
        path = project / str(line["audio"])
        if not path.is_file():
            raise VoiceError(f"line {line['id']}: {line['audio']} does not exist")
        return path
    for stem in (line["id"], f"line{line['id']}", f"line_{line['id']}"):
        for ext in AUDIO_EXTENSIONS:
            path = project / "assets" / "audio" / f"{stem}{ext}"
            if path.is_file():
                return path
    raise VoiceError(f"line {line['id']}: no recording found. Save its speech as assets/audio/{line['id']}.mp3 "
                     f"(or name the file in the line's \"audio\").")


def _take_path(line: dict[str, Any], part_id: str, part_count: int, index: int) -> str | None:
    take = line.get("take")
    if not take:
        return None
    if isinstance(take, str):
        if part_count == 1:
            return take
        stem, dot, ext = take.rpartition(".")
        return f"{stem}_{index + 1}{dot}{ext}" if dot else f"{take}_{index + 1}"
    return f"assets/video/take_{part_id}.mp4"


def prepare(project: Path, *, transcribe: Transcribe | None = None, model_size: str = "base") -> dict[str, Any]:
    """Prepare every line of the edit and write work/kit/voice.json. Returns the voice."""
    edit = json.loads((project / "composition" / "edit.json").read_text(encoding="utf-8"))
    spec = edit.get("voice") or {}
    lines = spec.get("lines") or []
    if not lines:
        raise VoiceError("the edit has no voice lines; add them under \"voice\": {\"lines\": [...]}")
    work = project / "work" / "kit"
    work.mkdir(parents=True, exist_ok=True)
    out_dir = project / "assets" / "audio"
    previous = {}
    try:
        previous = {l["id"]: l for l in json.loads((work / "voice.json").read_text(encoding="utf-8")).get("lines", [])}
    except (OSError, ValueError):
        pass
    run = transcribe or whisper_words(model_size, spec.get("language"), work / "transcripts")
    gap = float(spec.get("gap") or 0)
    used_ids = {str(l["id"]) for l in lines}

    result: list[dict[str, Any]] = []
    notes: list[str] = []
    cursor = 0.0
    for line in lines:
        line_id = str(line["id"])
        text = str(line.get("text") or "").strip()
        recording = find_recording(project, line)
        source = hashlib.sha256(recording.read_bytes() + text.encode("utf-8") + json.dumps(line.get("take")).encode()).hexdigest()
        cached = previous.get(line_id)
        if cached and cached.get("source") == source and all((project / p["audio"]).is_file() for p in cached["parts"]):
            parts = [{**p, "words": [{**w, "start": w["start"] - cached["parts"][0]["start"], "end": w["end"] - cached["parts"][0]["start"]}
                                     for w in p["words"]]} for p in cached["parts"]]
            local = [{"id": p["id"], "audio": p["audio"], "take": p.get("take"), "dur": p["dur"],
                      "offset": p["start"] - cached["parts"][0]["start"], "words": p["words"]} for p in parts]
        else:
            whole = trim(recording, work / "voice" / f"{line_id}.wav")
            total = duration_of(whole)
            words, heard_share = conform(script_tokens(text), run(whole), total)
            if heard_share < 0.6:
                notes.append(f"line {line_id}: only {heard_share:.0%} of the script's words were heard in the recording - "
                             f"check that the recording says the script.")
            cuts = split_points(words, total) if line.get("take") else []
            edges = [0.0, *cuts, total]
            local = []
            for index, (a, b) in enumerate(zip(edges, edges[1:])):
                part_id = line_id if len(edges) == 2 else f"{line_id}_{index + 1}"
                while len(edges) > 2 and part_id in used_ids:
                    part_id += "x"
                used_ids.add(part_id)
                audio = out_dir / f"voice_{part_id}.wav"
                audio.parent.mkdir(parents=True, exist_ok=True)
                if len(edges) == 2:
                    audio.write_bytes(whole.read_bytes())
                else:
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(whole), "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
                                    "-ar", "48000", "-ac", "1", str(audio)], check=True, timeout=120)
                part_words = [w for w in words if a - 1e-6 <= w["start"] < b - 1e-6 or (index == len(edges) - 2 and w["start"] >= b - 1e-6)]
                local.append({"id": part_id, "audio": audio.relative_to(project).as_posix(),
                              "take": _take_path(line, part_id, len(edges) - 1, index),
                              "dur": round(duration_of(audio), 3), "offset": a, "words": part_words})
            if cuts:
                notes.append(f"line {line_id} runs {total:.1f} s, over the {TAKE_LIMIT_SECONDS} s one take holds: split at "
                             f"{', '.join(f'{c:.1f} s' for c in cuts)} into {', '.join(p['id'] for p in local)}.")

        parts_out = []
        for part in local:
            start = round(cursor, 3)
            parts_out.append({
                "id": part["id"], "start": start, "dur": part["dur"], "audio": part["audio"], "take": part["take"],
                "words": [{"text": w["text"], "start": round(start + w["start"] - part["offset"], 3),
                           "end": round(start + min(w["end"] - part["offset"], part["dur"]), 3)} for w in part["words"]],
            })
            cursor += part["dur"]
        cursor += gap
        result.append({"id": line_id, "text": text, "source": source, "parts": parts_out})

    voice = {"lines": result, "voice_end": round(cursor - gap, 3), "notes": notes}
    (work / "voice.json").write_text(json.dumps(voice, indent=1, ensure_ascii=False), encoding="utf-8")
    return voice


def summary(voice: dict[str, Any]) -> str:
    rows = []
    for line in voice["lines"]:
        for part in line["parts"]:
            take = f" -> take {part['take']}" if part.get("take") else ""
            rows.append(f"  {part['id']:<8} {part['start']:6.2f} s  {part['dur']:5.2f} s  {part['audio']}{take}")
    takes = sum(1 for line in voice["lines"] for part in line["parts"] if part.get("take"))
    text = [f"Voice: {voice['voice_end']:.2f} s in {sum(len(l['parts']) for l in voice['lines'])} part(s).", *rows]
    if takes:
        text.append(f"Generate each of the {takes} take(s) from its prepared audio exactly as it is (trim_silence off): "
                    f"the timings below belong to these files.")
    text.extend(f"NOTE {note}" for note in voice.get("notes", []))
    return "\n".join(text)
