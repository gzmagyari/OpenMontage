"""The kit's cutter: one recording of a whole script, cut into its lines (lib/videokit/cut.py).

The words come from the script and the clock from a transcriber, as in `kit voice`; the cut
itself is moved into the pause the recording really has between two lines. Most tests give the
cutter a loudness curve and a clock directly; the last ones cut real sound made by ffmpeg.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from lib.videokit import cut, voice

FFMPEG = shutil.which("ffmpeg")
LOUD, QUIET = -20.0, -70.0


def _levels(total: float, *speech: tuple[float, float]) -> list[float]:
    """A recording's loudness every 10 ms: loud inside the given stretches, quiet elsewhere."""
    return [LOUD if any(a - 1e-9 <= index / 100 < b - 1e-9 for a, b in speech) else QUIET
            for index in range(int(round(total * 100)))]


def _heard(*words: tuple[str, float, float]) -> list[dict]:
    return [{"word": word, "start": start, "end": end} for word, start, end in words]


def _lines(*texts: str) -> list[dict]:
    return [{"id": chr(65 + index), "text": text, "output_path": f"{chr(65 + index)}.mp3"} for index, text in enumerate(texts)]


# Three lines, as a designed voice says them: speech, a pause, speech, a pause, speech.
SPEECH = ((0.05, 2.0), (2.5, 5.0), (5.4, 7.0))
SCRIPT = ("Stop scrolling now!", "Lumabrew makes it real, in a minute.", "Try it today!")
CLOCK = (("Stop", 0.06, 0.5), ("scrolling", 0.5, 1.2), ("now", 1.2, 1.7),
         ("Lumabrew", 2.45, 3.3), ("makes", 3.3, 3.6), ("it", 3.6, 3.7), ("real", 3.7, 4.1), ("in", 4.2, 4.3),
         ("a", 4.3, 4.35), ("minute", 4.35, 4.8),
         ("Try", 5.38, 5.7), ("it", 5.7, 5.9), ("today", 5.9, 6.7))


# ---------------------------------------------------------------- which edges the clock gives

def test_the_edges_of_a_word_are_heard_or_only_shared():
    tokens = voice.script_tokens("Be honest: it grows 4x bigger, really.")
    heard = _heard(("be", 0.0, 0.2), ("honest", 0.2, 0.6), ("it", 0.8, 0.9), ("grows", 0.9, 1.3),
                   ("four", 1.3, 1.6), ("times", 1.6, 1.9), ("bigger", 1.9, 2.4))
    words, share, edges = voice.conform_edges(tokens, heard, 3.0)
    assert (words, share) == voice.conform(tokens, heard, 3.0), "the same timings as `kit voice` uses"
    by = dict(zip(tokens, edges))
    assert by["honest:"] == (True, True)
    assert by["4x"] == (True, True), "written differently, but the stretch it was heard in is known"
    assert by["really."] == (False, False), "nobody heard it: it only shares what is left"
    # Two words heard as something else: the outer edges of the stretch are known, the inner are not.
    tokens = voice.script_tokens("upload it there now")
    _, _, edges = voice.conform_edges(tokens, _heard(("upload", 0, 1), ("id", 1, 2), ("their", 2, 3), ("now", 3, 4)), 4.0)
    assert edges == [(True, True), (True, False), (False, True), (True, True)]


# ---------------------------------------------------------------- where the lines lie

def test_each_cut_falls_in_the_pause_the_recording_has_between_two_lines():
    found = cut.find_lines(_levels(7.2, *SPEECH), 7.2, _lines(*SCRIPT), _heard(*CLOCK))
    assert [(p["start"], p["end"]) for p in found["pauses"]] == [(2.0, 2.5), (5.0, 5.4)]
    spans = [(line["id"], line["start"], line["end"]) for line in found["lines"]]
    # Every line keeps a breath of the pause before it and a little more of the one after it.
    assert spans == [("A", 0.0, 2.2), ("B", 2.44, 5.2), ("C", 5.34, 7.2)]
    assert found["heard"] == 1.0 and all(line["heard"] == 1.0 for line in found["lines"])
    # No cut touches a word: every stretch of speech lies whole inside its line.
    for (a, b), line in zip(SPEECH, found["lines"]):
        assert line["start"] <= a and b <= line["end"]


def test_the_clock_may_be_off_at_a_pause():
    """A transcriber often ends a word early, and starts the word after a pause where the word
    before it ended. The cut still lands in the quiet."""
    clock = [(w, 1.7, e) if w == "Lumabrew" else (w, 5.0, e) if w == "Try" else (w, s, e) for w, s, e in CLOCK]
    found = cut.find_lines(_levels(7.2, *SPEECH), 7.2, _lines(*SCRIPT), _heard(*clock))
    assert [(p["start"], p["end"]) for p in found["pauses"]] == [(2.0, 2.5), (5.0, 5.4)]


def test_the_longest_quiet_between_two_lines_is_the_pause():
    """The stop of a "k" or a "t" is quiet too, for a twentieth of a second."""
    speech = ((0.05, 1.55), (1.61, 2.0), (2.5, 2.62), (2.68, 5.0), (5.4, 7.0))
    found = cut.find_lines(_levels(7.2, *speech), 7.2, _lines(*SCRIPT), _heard(*CLOCK))
    assert (found["pauses"][0]["start"], found["pauses"][0]["end"]) == (2.0, 2.5)


def test_lines_that_run_into_each_other_are_refused():
    speech = ((0.05, 5.0), (5.4, 7.0))  # no pause between A and B
    with pytest.raises(cut.CutError, match="no pause between line A and line B"):
        cut.find_lines(_levels(7.2, *speech), 7.2, _lines(*SCRIPT), _heard(*CLOCK))


def test_a_recording_that_does_not_say_the_script_is_refused():
    levels = _levels(7.2, *SPEECH)
    # One line is missing: its words were never said.
    without_b = [row for row in CLOCK if not 2.4 < row[1] < 5.0]
    with pytest.raises(cut.CutError, match="line B was not heard in the recording"):
        cut.find_lines(levels, 7.2, _lines(*SCRIPT), _heard(*without_b))
    # It says something else.
    other = _heard(("Welcome", 0.1, 0.6), ("back", 0.6, 1.0), ("everyone", 1.0, 1.8), ("to", 2.5, 2.7), ("my", 2.7, 2.9),
                   ("channel", 2.9, 3.5), ("today", 5.9, 6.7))
    with pytest.raises(cut.CutError, match="line A was not heard in the recording"):
        cut.find_lines(levels, 7.2, _lines(*SCRIPT), other)
    # Half of every line is there, which is too little of the whole.
    halves = _heard(("one", 0.1, 0.5), ("two", 0.5, 1.0), ("five", 2.5, 3.0), ("six", 3.0, 3.6))
    with pytest.raises(cut.CutError, match="only 50% of the script's words were heard"):
        cut.find_lines(levels, 7.2, _lines("one two three four", "five six seven eight"), halves)
    with pytest.raises(cut.CutError, match="no words were heard"):
        cut.find_lines(levels, 7.2, _lines(*SCRIPT), [])
    with pytest.raises(cut.CutError, match="silent"):
        cut.find_lines(_levels(7.2), 7.2, _lines(*SCRIPT), _heard(*CLOCK))


def test_a_line_said_with_more_than_its_words_is_refused():
    """A designed voice that adds a sentence of its own would put it into a take."""
    added = (*CLOCK, ("And", 6.8, 6.9), ("subscribe", 6.9, 7.4), ("for", 7.4, 7.5), ("more", 7.5, 7.7),
             ("every", 7.7, 7.9), ("single", 7.9, 8.2), ("day", 8.2, 8.5))
    levels = _levels(8.7, SPEECH[0], SPEECH[1], (5.4, 8.5))
    with pytest.raises(cut.CutError, match="line C is said with words the script does not have"):
        cut.find_lines(levels, 8.7, _lines(*SCRIPT), _heard(*added))
    # A word or a number heard in other letters is not "more".
    spelled = [("luma", 2.45, 2.8) if w == "Lumabrew" else (w, s, e) for w, s, e in CLOCK]
    spelled.insert(4, ("brew", 2.8, 3.3))
    assert cut.find_lines(_levels(7.2, *SPEECH), 7.2, _lines(*SCRIPT), _heard(*spelled))["lines"][1]["heard"] > 0.9


def test_a_line_end_nobody_heard_is_refused():
    """Where one line ends and the next begins, at least one of the two words must be heard."""
    blurred = [("no", 1.2, 1.5) if w == "now" else ("for", 2.45, 2.7) if w == "Lumabrew" else (w, s, e)
               for w, s, e in CLOCK]
    blurred.insert(4, ("a", 2.7, 2.8))
    blurred.insert(5, ("brew", 2.8, 3.3))
    with pytest.raises(cut.CutError, match="end of line A and the start of line B were not heard clearly"):
        cut.find_lines(_levels(7.2, *SPEECH), 7.2, _lines(*SCRIPT), _heard(*blurred))


def test_a_script_without_spaces_is_cut_too():
    lines = _lines("今日は良い天気です。", "明日も晴れるでしょう。")
    heard = _heard(("今日は良い", 0.1, 1.0), ("天気です", 1.0, 1.9), ("明日も", 2.5, 3.2), ("晴れるでしょう", 3.2, 4.4))
    found = cut.find_lines(_levels(4.6, (0.1, 1.9), (2.5, 4.4)), 4.6, lines, heard)
    assert (found["pauses"][0]["start"], found["pauses"][0]["end"]) == (1.9, 2.5)
    assert [line["id"] for line in found["lines"]] == ["A", "B"]


# ---------------------------------------------------------------- real sound

def _tone(path: Path, *parts: tuple[str, float]) -> None:
    """A recording of tones ("330") and silences ("-"), as MP3."""
    inputs: list[str] = []
    for kind, seconds in parts:
        source = f"anullsrc=r=44100:cl=mono:d={seconds}" if kind == "-" else f"sine=frequency={kind}:duration={seconds}:sample_rate=44100"
        inputs += ["-f", "lavfi", "-i", source]
    graph = "".join(f"[{index}:a]" for index in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", graph, "-c:a", "libmp3lame", "-b:a", "128k",
                    str(path)], check=True, timeout=120)


def _sound(path: Path) -> tuple[float, float, float]:
    """A file's length, and where its sound begins and ends."""
    pcm = cut._samples(path)
    begin, end = cut._speech_edges(cut._levels(pcm))
    return len(pcm) / cut.RATE, begin, end


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_real_recording_is_cut_into_one_file_a_line(tmp_path: Path):
    recording = tmp_path / "recording.mp3"
    _tone(recording, ("-", 0.1), ("330", 1.9), ("-", 0.5), ("440", 2.5), ("-", 0.4), ("550", 1.6), ("-", 0.2))
    lines = [{**line, "output_path": str(tmp_path / "audio" / line["output_path"])} for line in _lines(*SCRIPT)]
    asked: list[Path] = []
    clock = [(w, s + 0.05, e + 0.05) for w, s, e in CLOCK]

    def transcribe(path: Path):
        asked.append(path)
        return _heard(*clock)

    found = cut.cut_lines(recording, lines, transcribe=transcribe)
    assert asked == [recording] and [line["id"] for line in found["lines"]] == ["A", "B", "C"]
    for line, tone in zip(lines, (1.9, 2.5, 1.6)):
        length, begin, end = _sound(Path(line["output_path"]))
        assert end - begin == pytest.approx(tone, abs=0.05), "the whole line, and nothing of its neighbours"
        assert begin <= cut.LEAD + 0.06 and length - end <= cut.TAIL + 0.06
    assert sorted(p.name for p in (tmp_path / "audio").iterdir()) == ["A.mp3", "B.mp3", "C.mp3"]
    # `kit voice` takes the files as it takes any spoken line: its own trim keeps the whole tone.
    assert 2.5 <= voice.duration_of(voice.trim(Path(lines[1]["output_path"]), tmp_path / "B.wav")) <= 3.0


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_refused_recording_changes_no_file_and_a_line_without_a_path_is_left_out(tmp_path: Path):
    recording = tmp_path / "recording.mp3"
    _tone(recording, ("-", 0.1), ("330", 1.9), ("-", 0.5), ("440", 2.5), ("-", 0.4), ("550", 1.6), ("-", 0.2))
    audio = tmp_path / "audio"
    audio.mkdir()
    lines = [{**line, "output_path": str(audio / line["output_path"])} for line in _lines(*SCRIPT)]
    for line in lines:
        Path(line["output_path"]).write_bytes(b"OLD")
    clock = [(w, s + 0.05, e + 0.05) for w, s, e in CLOCK]

    with pytest.raises(cut.CutError, match="line B was not heard"):
        cut.cut_lines(recording, lines, transcribe=lambda path: _heard(*[row for row in clock if not 2.4 < row[1] < 5.0]))
    assert all(Path(line["output_path"]).read_bytes() == b"OLD" for line in lines)
    assert sorted(p.name for p in audio.iterdir()) == ["A.mp3", "B.mp3", "C.mp3"], "nothing half-written is left"
    with pytest.raises(cut.CutError, match="could not be timed"):
        cut.cut_lines(recording, lines, transcribe=lambda path: (_ for _ in ()).throw(RuntimeError("no model")))
    with pytest.raises(cut.CutError, match="could not be read"):
        cut.cut_lines(tmp_path / "missing.mp3", lines, transcribe=lambda path: _heard(*clock))

    # A line that is said but not wanted (a short script said twice) is found and not written.
    lines[2]["output_path"] = ""
    cut.cut_lines(recording, lines, transcribe=lambda path: _heard(*clock))
    assert Path(lines[0]["output_path"]).read_bytes() != b"OLD" and (audio / "C.mp3").read_bytes() == b"OLD"
