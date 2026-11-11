"""voiceover_tts: one voice for one video. A voice is designed for the presenter, saved, used for
every line and kept for the video; a later call speaks with it again. When no voice can be saved
the design's own recording of the whole script is cut into the lines, and when that cannot be had
either the lines are spoken by a stock voice. Never by two voices."""

from __future__ import annotations

import json
import math
import threading
import types
from pathlib import Path

import pytest

from lib.videokit import cut
from tests.tools.eleven_fake import FakeEleven, _error
from tools.audio import voiceover_tts
from tools.audio.fal_elevenlabs_tts import FalElevenLabsTTS
from tools.audio.voiceover_tts import GONE, ONCE, VoiceoverTTS
from tools.base_tool import ToolResult, ToolStatus, _budget_quote

TEXTS = {
    "A": "Your morning coffee tastes flat. Lumabrew fixes that.",
    "B": "Drop a pod in, press once, and about a minute later it is a real cafe cup, crema and all.",
    "C": "This is not instant coffee. It grinds the beans first, so the smell fills the kitchen.",
}
FIRST = {"voice_description": "A man in his mid sixties from the American Midwest. Warm, slightly gravelly voice.",
         "gender": "man", "age": "60s"}


def usd(units: float, rate: float = 0.35) -> float:
    """ElevenLabs' units in dollars, at the default rate a thousand."""
    return units * rate / 1000


@pytest.fixture()
def eleven(monkeypatch):
    fake = FakeEleven()
    monkeypatch.setattr("requests.request", fake.request)
    monkeypatch.setenv("ELEVENLABS_VOICE_API_KEY", "eleven-test-key")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setenv("FAL_KEY", "fal-test-key")
    monkeypatch.setenv("OPENMONTAGE_INSTANCE_ID", "test")
    for name in ("ELEVENLABS_USD_PER_1K_UNITS", "OPENMONTAGE_PROJECT_DIR", "VOICEOVER_FIRST_CHOICE"):
        monkeypatch.delenv(name, raising=False)
    return fake


@pytest.fixture()
def cutter(monkeypatch):
    """The kit's cutter, without audio (tests/kit/test_voice_cut.py cuts real sound): each line's
    file says which candidate's recording it was cut from. `refuse` holds what the next
    recordings are answered with, one entry a recording: a reason refuses it, None cuts it."""
    state = types.SimpleNamespace(calls=[], refuse=[])

    def cut_lines(recording, lines, *, language=None, work_dir=None, **_):
        body = Path(recording).read_bytes()
        candidate = body[4:body.index(b"]")].decode()
        state.calls.append({"candidate": candidate, "lines": [dict(line) for line in lines], "language": language,
                            "recording": body})
        reason = state.refuse.pop(0) if state.refuse else None
        if reason:
            raise cut.CutError(reason)
        for line in lines:
            if line["output_path"]:
                Path(line["output_path"]).parent.mkdir(parents=True, exist_ok=True)
                Path(line["output_path"]).write_bytes(f"CUT[{candidate}]{line['text']}".encode())
        return {"seconds": 12.0, "heard": 1.0, "pauses": [],
                "lines": [{"id": line["id"], "start": 0.0, "end": 1.0, "seconds": 1.0, "heard": 1.0} for line in lines]}

    monkeypatch.setattr(cut, "cut_lines", cut_lines)
    return state


@pytest.fixture()
def stock(monkeypatch):
    """fal's stock-voice tool, without fal: it writes the file and reports fal's price and id."""
    calls: list[dict] = []
    lock = threading.Lock()

    def execute(self, inputs):
        with lock:
            calls.append(dict(inputs))
            number = len(calls)
        path = Path(inputs["output_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"STOCK[{inputs['voice']}]{inputs['text']}".encode())
        return ToolResult(success=True, cost_usd=round(len(inputs["text"]) * 0.0001, 6),
                          data={"request_ids": [f"fal-{number}"]})

    monkeypatch.setattr(FalElevenLabsTTS, "execute", execute)
    return calls


@pytest.fixture()
def no_waiting(monkeypatch):
    monkeypatch.setattr(voiceover_tts.time, "sleep", lambda seconds: None)


def _project(tmp_path, name="job_abc123") -> Path:
    project = tmp_path / name
    (project / "assets" / "audio").mkdir(parents=True)
    return project


def _lines(project: Path, ids: str = "ABC") -> list[dict]:
    return [{"id": i, "text": TEXTS[i], "output_path": str(project / "assets" / "audio" / f"{i}.mp3")} for i in ids]


def _record(project: Path) -> dict:
    return json.loads((project / "artifacts" / "voice.json").read_text(encoding="utf-8"))


def _units(ids: str = "ABC", factor: float = 0.115) -> int:
    return sum(math.ceil(len(TEXTS[i]) * factor) for i in ids)


def _sample() -> str:
    """What the design is given for this script: its start, up to the first sentence end past 100
    characters (the recording is not used, and every character of it is counted)."""
    sample = f"{TEXTS['A']} {TEXTS['B']}"
    assert len(TEXTS["A"]) < 100 < len(sample) == 143
    return sample


def _script(ids: str = "ABC") -> str:
    """What a design is given when its own recording is to be the voice: the whole script, one
    line a paragraph."""
    return "\n\n".join(TEXTS[i] for i in ids)


# -- the first call ------------------------------------------------------------------------

def test_the_first_call_designs_a_voice_saves_it_and_speaks_every_line(eleven, stock, tmp_path):
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project), "language": "en"})

    assert result.success, result.error
    assert eleven.paths("POST")[:2] == ["/v1/text-to-voice/design", "/v1/text-to-voice"]
    (voice_id, voice), = eleven.voices.items()
    # Named after this installation and the video, and labelled the same way.
    assert voice["name"] == "om-test-job_abc123"
    assert voice["labels"]["om_project"] == "job_abc123" and voice["labels"]["om_instance"] == "om-test"
    assert voice["generated_voice_id"].endswith("-0"), "the first candidate is the one that is saved"
    # Every line was spoken with that voice on Eleven v4 Turbo, and the files are its audio.
    assert sorted(eleven.speech) == sorted((voice_id, "eleven_v4_turbo", TEXTS[i]) for i in "ABC")
    for line in _lines(project):
        assert Path(line["output_path"]).read_bytes() == f"MP3[{voice_id}]{line['text']}".encode()
    # The voice is KEPT: nothing was removed, and the video's record says where it is.
    assert not eleven.paths("DELETE")
    record = _record(project)
    assert (record["kind"], record["voice_id"], record["model"]) == ("saved", voice_id, "v4-turbo-designed")
    assert record["gender"] == "man" and record["age"] == "60s" and record["voice_name"] == voice["name"]
    # The cost is ElevenLabs' own count: the sample text once, and what each speech answer said.
    units = len(_sample()) + _units()
    assert result.data["elevenlabs_units"] == units == eleven.character_count
    assert result.cost_usd == round(usd(units), 4)
    assert result.data["costs"] == {"design_usd": round(usd(len(_sample())), 4),
                                    "speech_usd": round(usd(_units()), 4), "stock_usd": 0.0}
    assert len(result.data["elevenlabs_request_ids"]) == 3
    assert result.data["request_ids"] == [], "fal's billing check is not asked about ElevenLabs ids"
    assert result.data["voice"] == {"kind": "saved", "reused": False, "voice_id": voice_id}
    assert result.data["provider"] == "elevenlabs" and result.model == "v4-turbo-designed"
    assert "fallback" not in result.data and "operator_note" not in result.data
    assert stock == [], "no stock voice was needed"


def test_two_lines_at_a_time_never_more(eleven, stock, tmp_path):
    """The smallest plan answers 429 above three requests at once; two leave one free."""
    project = _project(tmp_path)
    TEXTS.update({str(n): f"Line number {n} of a longer script that needs speaking." for n in range(6)})
    try:
        lines = _lines(project, "ABC") + _lines(project, "012345")
        assert VoiceoverTTS().execute({**FIRST, "lines": lines}).success
    finally:
        for n in range(6):
            TEXTS.pop(str(n))
    assert len(eleven.speech) == 9 and eleven.most_at_once <= 2


def test_gender_and_age_head_the_description_when_it_does_not_say_them(eleven, stock, tmp_path):
    project = _project(tmp_path)
    tool = VoiceoverTTS()
    assert tool.execute({"voice_description": "Bright, clear, friendly voice from California.", "gender": "woman",
                         "age": "early 30s", "lines": _lines(project)}).success
    assert next(iter(eleven.voices.values()))["description"] == \
        "A woman in her early 30s. Bright, clear, friendly voice from California."
    # A description that already says who is speaking is sent as it is.
    other = _project(tmp_path, "job_other")
    assert tool.execute({**FIRST, "lines": _lines(other)}).success
    assert [v["description"] for v in eleven.voices.values()][-1] == FIRST["voice_description"]


@pytest.mark.parametrize("gender, age, sentence", [
    ("woman", "early 30s", "A woman in her early 30s."), ("man", "60s", "A man in his 60s."),
    ("man", "mid sixties", "A man in his mid sixties."), ("woman", "twenties", "A woman in her twenties."),
    ("man", "45", "A 45 year old man."), ("woman", "45 years old", "A 45 year old woman."),
    ("man", "forty-five", "A forty-five year old man."), ("woman", "young", "A young woman."),
    ("man", "middle-aged", "A middle-aged man."), ("man", "elderly", "An elderly man."),
])
def test_the_sentence_that_says_who_is_speaking_reads_right(gender, age, sentence):
    assert VoiceoverTTS._who(gender, age) == sentence


def test_a_description_too_short_for_elevenlabs_is_headed_by_who_speaks(eleven, stock, tmp_path):
    # ElevenLabs takes no fewer than 20 characters; a director's "A man, deep voice." has 18.
    result = VoiceoverTTS().execute({"voice_description": "A man, deep voice.", "gender": "man", "age": "45",
                                     "lines": _lines(_project(tmp_path))})
    assert result.success and next(iter(eleven.voices.values()))["description"] == "A 45 year old man. A man, deep voice."


def test_v4_is_the_same_voice_path_at_its_own_rate(eleven, stock, tmp_path):
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project), "model": "v4-designed"})
    assert result.success and {model for _, model, _ in eleven.speech} == {"eleven_v4"}
    assert result.data["elevenlabs_units"] == len(_sample()) + _units(factor=0.23)
    assert _record(project)["model"] == "v4-designed"


# -- a later call ----------------------------------------------------------------------------

def test_a_later_call_speaks_in_the_videos_own_voice_without_a_new_design(eleven, stock, tmp_path):
    project = _project(tmp_path)
    tool = VoiceoverTTS()
    assert tool.execute({**FIRST, "lines": _lines(project)}).success
    voice_id = next(iter(eleven.voices))
    designs, creations = eleven.count("POST", "/v1/text-to-voice"), eleven.add_edit_counter

    changed = [{"id": "B", "text": "Drop a pod in and press once.", "output_path": str(project / "assets/audio/B.mp3")}]
    again = tool.execute({"lines": changed})

    assert again.success, again.error
    assert eleven.count("POST", "/v1/text-to-voice") == designs, "nothing was designed or saved again"
    assert eleven.add_edit_counter == creations == 1, "no second voice creation this month"
    assert eleven.speech[-1] == (voice_id, "eleven_v4_turbo", "Drop a pod in and press once.")
    assert again.data["voice"] == {"kind": "saved", "reused": True, "voice_id": voice_id}
    units = math.ceil(len("Drop a pod in and press once.") * 0.115)
    assert again.data["elevenlabs_units"] == units and again.cost_usd == round(usd(units), 4)
    assert again.data["costs"]["design_usd"] == 0.0


def test_a_saved_voice_is_found_by_its_name_when_the_record_is_lost(eleven, stock, tmp_path):
    project = _project(tmp_path)
    tool = VoiceoverTTS()
    assert tool.execute({**FIRST, "lines": _lines(project)}).success
    voice_id = next(iter(eleven.voices))
    (project / "artifacts" / "voice.json").unlink()

    again = tool.execute({"lines": _lines(project, "A")})
    assert again.success and again.data["voice"] == {"kind": "saved", "reused": True, "voice_id": voice_id}
    assert len(eleven.voices) == 1 and eleven.add_edit_counter == 1


def test_a_voice_that_is_gone_is_said_plainly_and_no_other_voice_speaks(eleven, stock, tmp_path):
    project = _project(tmp_path)
    tool = VoiceoverTTS()
    assert tool.execute({**FIRST, "lines": _lines(project)}).success
    eleven.voices.clear()  # removed to make room for newer videos, or with age

    again = tool.execute({"lines": _lines(project, "B")})
    assert not again.success and again.error == GONE
    assert "Leave the spoken words as they are" in again.error
    assert stock == [], "a line is never spoken in another voice"
    assert eleven.count("POST", "/v1/text-to-voice/design") == 1, "and no new voice was designed for one line"


def test_new_voice_replaces_the_videos_voice(eleven, stock, tmp_path):
    project = _project(tmp_path)
    tool = VoiceoverTTS()
    assert tool.execute({**FIRST, "lines": _lines(project)}).success
    first_id = next(iter(eleven.voices))
    again = tool.execute({**FIRST, "lines": _lines(project), "new_voice": True})
    assert again.success and again.data["voice"]["reused"] is False
    (second_id, _), = eleven.voices.items()
    assert second_id != first_id and _record(project)["voice_id"] == second_id
    assert eleven.add_edit_counter == 2


def test_a_video_without_a_voice_needs_the_description(eleven, stock, tmp_path):
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({"lines": _lines(project, "B")})
    assert not result.success and "no voice on record" in result.error
    assert "voice_description" in result.error and "fal_elevenlabs_tts" in result.error
    assert eleven.paths("POST") == [] and stock == []


# -- the fallback to a stock voice -----------------------------------------------------------

def test_without_an_elevenlabs_key_a_stock_voice_of_the_same_gender_and_age_speaks(eleven, stock, tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_VOICE_API_KEY")
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})

    assert result.success, result.error
    assert eleven.calls == [], "ElevenLabs was not asked at all"
    assert {call["voice"] for call in stock} == {"Bill"} and {call["model_id"] for call in stock} == {"eleven-v3"}
    assert result.data["voice"] == {"kind": "stock", "reused": False, "stock_voice": "Bill"}
    assert result.data["fallback"] == "this server has no ElevenLabs key"
    assert "stock voice Bill" in result.data["note"] and "stock voice Bill" in result.data["operator_note"]
    # fal's charge is the whole cost, so fal's ids go to the ledger and its billing check applies.
    assert sorted(result.data["request_ids"]) == ["fal-1", "fal-2", "fal-3"] and result.data["elevenlabs_units"] == 0
    assert result.cost_usd == round(sum(len(t) for t in TEXTS.values()) * 0.0001, 4)
    assert _record(project)["kind"] == "stock" and _record(project)["stock_voice"] == "Bill"
    # A later call keeps to that stock voice.
    again = VoiceoverTTS().execute({"lines": _lines(project, "A")})
    assert again.success and stock[-1]["voice"] == "Bill" and again.data["voice"]["reused"] is True


def test_an_account_without_characters_is_seen_before_anything_is_spent(eleven, stock, cutter, tmp_path):
    eleven.character_count = eleven.character_limit - 50
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})
    assert result.success and result.data["voice"]["kind"] == "stock"
    # Neither a saved voice nor the design's own recording fits into what is left, and each says so.
    assert "too few characters left (50, this script needs about" in result.data["fallback"]
    assert f"the recording needs {len(_script())}" in result.data["fallback"]
    assert eleven.paths("POST") == [], "no design was bought"
    assert eleven.character_count == eleven.character_limit - 50 and result.data["elevenlabs_units"] == 0
    assert len(stock) == 3 and result.data["request_ids"] and cutter.calls == []


def test_a_refused_description_goes_to_the_stock_voice_with_elevenlabs_own_words(eleven, stock, cutter, tmp_path):
    """The recording is a design of the same description: it is not asked for a second refusal."""
    eleven.design_error = _error(400, "voice_design_refused", "The description names a real person.")
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})
    assert result.success and "The description names a real person." in result.data["fallback"]
    assert "recording" not in result.data["fallback"]
    assert eleven.voices == {} and len(stock) == 3 and result.data["elevenlabs_units"] == 0
    assert eleven.count("POST", "/v1/text-to-voice/design") == 1 and cutter.calls == []


def test_an_account_that_cannot_be_read_goes_to_the_stock_voice(eleven, stock, cutter, tmp_path, monkeypatch):
    real = eleven.request
    monkeypatch.setattr("requests.request", lambda method, url, **kwargs: (
        _error(401, "invalid_api_key", "Invalid API key.") if url.endswith("/v1/user/subscription")
        else real(method, url, **kwargs)))
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.success and result.data["voice"]["kind"] == "stock" and "Invalid API key." in result.data["fallback"]
    assert eleven.paths("POST") == [] and cutter.calls == []


def test_lines_still_waiting_are_not_spoken_in_a_voice_that_will_not_be_used(eleven, stock, cutter, tmp_path, no_waiting):
    project = _project(tmp_path)
    TEXTS.update({str(n): f"Line number {n} of a longer script that needs speaking." for n in range(6)})
    try:
        eleven.speech_troubles = [0]
        eleven.speech_always = 500
        result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project, "ABC012345")})
    finally:
        for n in range(6):
            TEXTS.pop(str(n))
    assert result.success and result.data["voice"]["kind"] == "recording" and stock == []
    # One line worked, the two lines in flight were tried three times each; the other six never asked.
    assert eleven.count("POST", "/v1/text-to-speech/") <= 7 and len(eleven.speech) == 1


def test_a_later_call_that_fails_changes_no_file(eleven, stock, tmp_path, no_waiting):
    """A revision that changes two lines must not end with one of them changed."""
    project = _project(tmp_path)
    tool = VoiceoverTTS()
    assert tool.execute({**FIRST, "lines": _lines(project)}).success
    before = {line["output_path"]: Path(line["output_path"]).read_bytes() for line in _lines(project)}
    eleven.speech_troubles = [0]
    eleven.speech_always = 500
    changed = [{**line, "text": line["text"] + " Really."} for line in _lines(project, "AB")]

    again = tool.execute({"lines": changed})
    assert not again.success and "could not speak just now" in again.error and "No other voice was used" in again.error
    assert {path: Path(path).read_bytes() for path in before} == before
    assert stock == [] and len(eleven.voices) == 1, "the voice stays for another try"
    assert again.cost_usd and again.data["elevenlabs_units"] > 0, "what the line that worked cost is still counted"


def test_a_stock_voice_that_fails_midway_changes_no_file(eleven, tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_VOICE_API_KEY")
    project = _project(tmp_path)
    for line in _lines(project):
        Path(line["output_path"]).write_bytes(b"OLD")

    def execute(self, inputs):
        if inputs["text"] == TEXTS["B"]:
            return ToolResult(success=False, error="fal is down")
        Path(inputs["output_path"]).write_bytes(b"NEW")
        return ToolResult(success=True, cost_usd=0.001, data={"request_ids": ["fal-x"]})

    monkeypatch.setattr(FalElevenLabsTTS, "execute", execute)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})
    assert not result.success and "line B" in result.error and "fal is down" in result.error
    assert all(Path(line["output_path"]).read_bytes() == b"OLD" for line in _lines(project))
    assert sorted(p.name for p in (project / "assets" / "audio").iterdir()) == ["A.mp3", "B.mp3", "C.mp3"]
    assert not (project / "artifacts" / "voice.json").exists(), "a voice that spoke nothing is not the video's voice"


def test_too_many_requests_is_waited_out_not_treated_as_a_failure(eleven, stock, tmp_path, no_waiting):
    project = _project(tmp_path)
    eleven.speech_troubles = [429, 429, 429, 429, 429, 429]
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})
    assert result.success and "fallback" not in result.data and stock == []
    assert len(eleven.speech) == 3 and len(eleven.voices) == 1


def test_a_busy_account_is_waited_out_while_the_voice_is_designed_too(eleven, stock, tmp_path, no_waiting):
    """Two videos' voices made at the same moment share the account's three requests at once."""
    eleven.design_busy = 2
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.success and "fallback" not in result.data and stock == []
    assert eleven.count("POST", "/v1/text-to-voice/design") == 3 and len(eleven.voices) == 1
    # Asked three times, charged for the one that was answered.
    assert result.data["costs"]["design_usd"] == round(usd(len(_sample())), 4)


def test_each_step_has_a_time_limit(eleven, stock, cutter, tmp_path, no_waiting, monkeypatch):
    monkeypatch.setattr(VoiceoverTTS, "_DESIGNED_SECONDS", 0.0)
    eleven.speech_always = 429
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})
    # The saved voice ran out of time: the recording, which has its own, takes over.
    assert result.success and "in time" in result.data["fallback"] and eleven.voices == {}
    assert result.data["voice"]["kind"] == "recording" and stock == []
    # A recording that cannot be had in its time leaves the stock voice.
    monkeypatch.setattr(VoiceoverTTS, "_RECORDING_SECONDS", 0.0)
    eleven.design_busy = 99
    eleven.add_edit_counter = eleven.max_add_edits
    other = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path, "job_slow"))})
    assert other.success and other.data["voice"]["kind"] == "stock" and len(stock) == 3
    assert "used up; and the design's own recording could not be used" in other.data["fallback"]


def test_the_stock_voice_failing_too_is_a_failure_that_keeps_what_was_spent(eleven, tmp_path, monkeypatch):
    monkeypatch.setattr(FalElevenLabsTTS, "execute", lambda self, inputs: ToolResult(success=False, error="fal is down"))
    eleven.design_error = _error(500, "internal_error", "Something went wrong.")
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert not result.success and "stock voice could not speak" in result.error and "fal is down" in result.error


# -- the second choice: the design's own recording ---------------------------------------------

def test_when_the_months_voices_are_used_up_the_designs_own_recording_speaks(eleven, stock, cutter, tmp_path):
    """Saving a voice counts against the month; a design that saves nothing does not. So the
    design speaks the whole script itself and the recording is cut into the lines."""
    eleven.add_edit_counter = eleven.max_add_edits
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project), "language": "en"})

    assert result.success, result.error
    # ONE paid call: a design of the same description with the whole script, a line a paragraph.
    assert eleven.paths("POST") == ["/v1/text-to-voice/design"]
    assert eleven.designs == [{"voice_description": FIRST["voice_description"], "text": _script()}]
    assert eleven.voices == {} and eleven.add_edit_counter == eleven.max_add_edits, "nothing was saved"
    assert eleven.speech == [] and stock == []
    # Every line is cut from the FIRST candidate's recording, and that recording is kept.
    candidate = cutter.calls[0]["candidate"]
    assert len(cutter.calls) == 1 and candidate.endswith("-0") and cutter.calls[0]["language"] == "en"
    assert [(line["id"], line["text"]) for line in cutter.calls[0]["lines"]] == [(i, TEXTS[i]) for i in "ABC"]
    for line in _lines(project):
        assert Path(line["output_path"]).read_bytes() == f"CUT[{candidate}]{line['text']}".encode()
    assert (project / "artifacts" / "voice_recording.mp3").read_bytes() == cutter.calls[0]["recording"]
    record = _record(project)
    assert (record["kind"], record["model"], record["candidate"]) == ("recording", "design-recording", 0)
    assert record["gender"] == "man" and [line["id"] for line in record["lines"]] == ["A", "B", "C"]
    assert "voice creations on the ElevenLabs account are used up" in record["fallback"]
    # The cost is the design's text, counted once, and the line is ElevenLabs' own.
    assert result.data["elevenlabs_units"] == len(_script()) == eleven.character_count
    assert result.cost_usd == round(usd(len(_script())), 4)
    assert result.data["costs"] == {"design_usd": round(usd(len(_script())), 4), "speech_usd": 0.0, "stock_usd": 0.0}
    assert result.data["provider"] == "elevenlabs" and result.model == "design-recording"
    assert result.data["voice"] == {"kind": "recording", "reused": False} and result.data["request_ids"] == []
    # The director is told what it has; the operator is told why.
    assert "cannot speak again" in result.data["note"] and "used up" in result.data["note"]
    assert result.data["operator_note"].startswith("voiceover_tts used the design's own recording: this month's voice")
    assert "stock" not in result.data["note"]


def test_a_voice_that_could_not_speak_every_line_gives_way_to_the_recording(eleven, stock, cutter, tmp_path, no_waiting):
    """Two voices in one video is the one thing that must not happen."""
    project = _project(tmp_path)
    eleven.speech_troubles = [0, 500, 500, 500, 500]  # the first line works, then ElevenLabs breaks
    eleven.speech_always = 500
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})

    assert result.success, result.error
    for line in _lines(project):
        assert Path(line["output_path"]).read_bytes().startswith(b"CUT["), "no line kept the saved voice"
    assert eleven.voices == {}, "a voice that could not be used is not kept"
    assert _record(project)["kind"] == "recording" and stock == []
    # All of it was charged: the first design, the line that was spoken, the second design.
    assert len(eleven.speech) >= 1
    assert result.data["elevenlabs_units"] > len(_sample()) + len(_script())
    assert result.cost_usd == round(usd(result.data["elevenlabs_units"]), 4)


def test_when_the_recording_cannot_be_had_either_the_stock_voice_speaks_every_line(eleven, stock, cutter, tmp_path, no_waiting):
    project = _project(tmp_path)
    eleven.speech_troubles = [0]
    eleven.speech_always = 500
    eleven.recordings = False  # the second design comes back without sound
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})

    assert result.success, result.error
    assert sorted(call["text"] for call in stock) == sorted(TEXTS[i] for i in "ABC")
    for line in _lines(project):
        assert Path(line["output_path"]).read_bytes().startswith(b"STOCK[Bill]")
    assert _record(project)["kind"] == "stock" and eleven.voices == {} and cutter.calls == []
    assert "and the design's own recording could not be used: the voice design returned no recording" in result.data["fallback"]
    # Three steps were paid for, so the cost is their sum and stays the tool's own figure.
    assert result.data["elevenlabs_units"] > len(_sample()) + len(_script())
    assert result.data["costs"]["stock_usd"] > 0 and result.data["request_ids"] == []
    assert result.cost_usd == round(usd(result.data["elevenlabs_units"]) + result.data["costs"]["stock_usd"], 4)
    assert result.data["operator_note"].startswith("voiceover_tts used the stock voice Bill:")


def test_the_first_recording_that_can_be_cut_is_the_videos_voice(eleven, stock, cutter, tmp_path):
    """A design returns three candidates, each with its own recording, for one price."""
    eleven.add_edit_counter = eleven.max_add_edits
    cutter.refuse = ["there is no pause between line A and line B", None]
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})
    assert result.success and result.data["voice"]["kind"] == "recording"
    assert [call["candidate"][-2:] for call in cutter.calls] == ["-0", "-1"]
    assert _record(project)["candidate"] == 1
    assert Path(_lines(project)[0]["output_path"]).read_bytes().startswith(f"CUT[{cutter.calls[1]['candidate']}]".encode())
    assert eleven.count("POST", "/v1/text-to-voice/design") == 1, "one design, three recordings"


def test_recordings_that_cannot_be_cut_leave_the_stock_voice_and_are_still_paid_for(eleven, stock, cutter, tmp_path):
    eleven.add_edit_counter = eleven.max_add_edits
    cutter.refuse = ["line B was not heard in the recording (20% of its words)",
                     "there is no pause between line A and line B", "there is no pause between line A and line B"]
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})
    assert result.success and result.data["voice"]["kind"] == "stock" and len(stock) == 3 and len(cutter.calls) == 3
    reason = result.data["fallback"]
    assert reason.startswith("this month's voice creations on the ElevenLabs account are used up; and the design's own")
    assert "line B was not heard" in reason and reason.count("there is no pause between line A and line B") == 1
    assert not (project / "artifacts" / "voice_recording.mp3").exists()
    assert result.data["elevenlabs_units"] == len(_script())
    assert result.cost_usd == round(usd(len(_script())) + result.data["costs"]["stock_usd"], 4)


def test_a_script_longer_than_a_design_speaks_goes_to_the_stock_voice(eleven, stock, cutter, tmp_path):
    eleven.add_edit_counter = eleven.max_add_edits
    project = _project(tmp_path)
    lines = [{"id": str(n), "text": "This sentence is forty-five characters long. " * 3,
              "output_path": str(project / "assets" / "audio" / f"{n}.mp3")} for n in range(8)]
    result = VoiceoverTTS().execute({**FIRST, "lines": lines})
    assert result.success and result.data["voice"]["kind"] == "stock" and len(stock) == 8
    assert "longer than a design can speak" in result.data["fallback"] and "1000 at most" in result.data["fallback"]
    assert eleven.paths("POST") == [] and cutter.calls == [], "nothing was bought for a recording that cannot be"


def test_a_design_that_fails_while_recording_leaves_the_stock_voice(eleven, stock, cutter, tmp_path):
    eleven.add_edit_counter = eleven.max_add_edits
    eleven.design_error = _error(500, "internal_error", "Something went wrong.")
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.success and result.data["voice"]["kind"] == "stock" and len(stock) == 3
    assert "used up; and the design's own recording could not be used" in result.data["fallback"]
    assert "Something went wrong." in result.data["fallback"] and result.data["elevenlabs_units"] == 0


def test_too_many_requests_is_waited_out_while_the_recording_is_made(eleven, stock, cutter, tmp_path, no_waiting):
    eleven.add_edit_counter = eleven.max_add_edits
    eleven.design_busy = 2
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.success and result.data["voice"]["kind"] == "recording" and stock == []
    assert eleven.count("POST", "/v1/text-to-voice/design") == 3 and result.data["elevenlabs_units"] == len(_script())


def test_a_recorded_voice_cannot_speak_again_and_says_so(eleven, stock, cutter, tmp_path):
    eleven.add_edit_counter = eleven.max_add_edits
    project = _project(tmp_path)
    tool = VoiceoverTTS()
    assert tool.execute({**FIRST, "lines": _lines(project)}).success
    before = {line["output_path"]: Path(line["output_path"]).read_bytes() for line in _lines(project)}
    asked, spent = len(eleven.calls), eleven.character_count

    changed = [{**_lines(project, "B")[0], "text": "Drop a pod in and press once."}]
    again = tool.execute({"lines": changed})
    assert not again.success and again.error == ONCE
    assert "Leave the spoken words as they are" in again.error and "new_voice: true" in again.error
    assert {path: Path(path).read_bytes() for path in before} == before
    assert len(eleven.calls) == asked and eleven.character_count == spent and stock == []
    assert not again.cost_usd, "a refusal costs nothing"
    # A new month: `new_voice` gives the video a voice of its own after all, every line again.
    eleven.add_edit_counter = 0
    fresh = tool.execute({**FIRST, "lines": _lines(project), "new_voice": True})
    assert fresh.success and fresh.data["voice"]["kind"] == "saved" and _record(project)["kind"] == "saved"
    assert tool.execute({"lines": changed}).success, "and that voice does speak again"


def test_a_server_can_make_the_recording_its_first_choice(eleven, stock, cutter, tmp_path, monkeypatch):
    """VOICEOVER_FIRST_CHOICE=recording: no voice is ever saved, so no month's limit and no slot;
    the price is the whole script as the design's text, and the quote says so before the call."""
    monkeypatch.setenv("VOICEOVER_FIRST_CHOICE", "recording")
    project = _project(tmp_path)
    tool = VoiceoverTTS()
    first = {**FIRST, "lines": _lines(project)}
    assert tool.estimate_cost(first) == round(usd(len(_script())), 4)
    assert tool.estimate_cost({"lines": _lines(project, "B")}) == round(usd(math.ceil(len(TEXTS["B"]) * 0.115)), 4)
    result = tool.execute(first)
    assert result.success and result.data["voice"] == {"kind": "recording", "reused": False}
    assert eleven.paths("POST") == ["/v1/text-to-voice/design"] and eleven.voices == {} and eleven.add_edit_counter == 0
    assert result.cost_usd == tool.estimate_cost(first)
    # The operator chose it: it is not a fallback, and no warning is raised for it.
    assert "fallback" not in result.data and "operator_note" not in result.data and "fallback" not in _record(project)
    assert "cannot speak again" in result.data["note"]
    # A script too long for a design is quoted, and spoken, as the stock voice.
    long = [{"id": str(n), "text": "This sentence is forty-five characters long. " * 3,
             "output_path": str(project / "assets" / "audio" / f"long{n}.mp3")} for n in range(8)]
    assert tool.estimate_cost({**FIRST, "lines": long}) == round(sum(len(line["text"].strip()) for line in long) * 0.0001, 4)


def test_a_script_shorter_than_a_design_takes_is_said_again_and_cut_from_its_first_saying(eleven, stock, cutter, tmp_path):
    eleven.add_edit_counter = eleven.max_add_edits
    project = _project(tmp_path)
    lines = [{"id": "A", "text": "Fresh bread every morning.", "output_path": str(project / "assets" / "audio" / "A.mp3")},
             {"id": "B", "text": "Come and smell it.", "output_path": str(project / "assets" / "audio" / "B.mp3")}]
    result = VoiceoverTTS().execute({**FIRST, "lines": lines})
    assert result.success and result.data["voice"]["kind"] == "recording"
    script = "Fresh bread every morning.\n\nCome and smell it."
    assert eleven.designs[0]["text"] == "\n\n".join([script] * 3) and len(script) < 100 <= len(eleven.designs[0]["text"])
    # The cutter is told every line that is said; only the first saying has files.
    given = cutter.calls[0]["lines"]
    assert [line["text"] for line in given] == [line["text"] for line in lines] * 3
    assert [bool(line["output_path"]) for line in given] == [True, True, False, False, False, False]
    assert sorted(p.name for p in (project / "assets" / "audio").iterdir()) == ["A.mp3", "B.mp3"]
    assert [line["id"] for line in _record(project)["lines"]] == ["A", "B"]


@pytest.mark.parametrize("given, told", [("es", "es"), ("pt-BR", "pt"), ("EN", "en"), ("English", None), ("", "en")])
def test_the_cutter_is_told_the_language_only_as_a_code(eleven, stock, cutter, tmp_path, given, told):
    eleven.add_edit_counter = eleven.max_add_edits
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path)), "language": given})
    assert result.success and cutter.calls[0]["language"] == told


# -- room for a new voice --------------------------------------------------------------------

def test_a_full_account_gives_up_its_oldest_voice_of_ours_and_nothing_else(eleven, stock, tmp_path):
    owner = eleven.add_voice("The owner's own narrator", age_seconds=9_000_000, category="professional")
    other = eleven.add_voice("om-server-job_theirs", age_seconds=8_000_000)  # another installation's
    older = eleven.add_voice("om-test-job_older", age_seconds=7200)
    assert len(eleven.voices) == eleven.voice_limit == 3
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})

    assert result.success and "fallback" not in result.data
    assert older not in eleven.voices and owner in eleven.voices and other in eleven.voices
    assert result.data["made_room"] == "job_older"
    assert any(v["name"] == "om-test-job_abc123" for v in eleven.voices.values())


def test_a_voice_that_may_be_speaking_is_never_removed_to_make_room(eleven, stock, cutter, tmp_path):
    for n in range(3):
        eleven.add_voice(f"om-test-job_busy{n}", age_seconds=60)  # saved a minute ago
    before = set(eleven.voices)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.success and "no voice slot is free" in result.data["fallback"]
    assert set(eleven.voices) == before and stock == []
    # Nothing was designed that had no place: the one design is the recording's, which needs none.
    assert [design["text"] for design in eleven.designs] == [_script()]
    assert result.data["voice"]["kind"] == "recording" and eleven.count("POST", "/v1/text-to-voice") == 1


def test_a_slot_taken_in_between_is_made_room_for_once(eleven, stock, tmp_path):
    old = eleven.add_voice("om-test-job_older", age_seconds=7200)
    eleven.add_voice("The owner's own narrator", age_seconds=9_000_000, category="professional")
    # One slot is free when the call starts; another video takes it while this voice is designed.
    taken: list[str] = []
    eleven.after_design = lambda: taken or taken.append(eleven.add_voice("om-server-job_theirs"))
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.success and "fallback" not in result.data, result.data.get("fallback")
    assert old not in eleven.voices and taken[0] in eleven.voices
    assert result.data["made_room"] == "job_older" and eleven.count("POST", "/v1/text-to-voice/design") == 1


def test_the_no_slot_answer_alone_is_enough_to_make_room(eleven, stock, cutter, tmp_path):
    """The answer to the save is what is acted on, so a count of saved voices that lagged would
    not cost a video its saved voice."""
    old = eleven.add_voice("om-test-job_older", age_seconds=7200)
    eleven.save_errors = [eleven.no_slot()]  # refused although the account still shows room
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.success and "fallback" not in result.data and result.data["made_room"] == "job_older"
    assert old not in eleven.voices and len(eleven.voices) == 1
    # With nothing of ours old enough to give way, the refusal is the reason given.
    eleven.add_voice("The owner's own narrator", category="professional")
    eleven.save_errors = [eleven.no_slot()]
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path, "job_next"))})
    assert result.success and "maximum amount of custom voices" in result.data["fallback"]
    assert len(eleven.voices) == 2 and result.data["voice"]["kind"] == "recording" and stock == []


def test_a_save_refused_for_another_reason_removes_no_voice(eleven, stock, cutter, tmp_path):
    old = eleven.add_voice("om-test-job_older", age_seconds=7200)
    eleven.save_errors = [_error(500, "internal_error", "Something went wrong.")]
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.success and "Something went wrong." in result.data["fallback"]
    assert list(eleven.voices) == [old], "an older video keeps its voice when the slot was not the problem"
    # The voice was designed and could not be kept: a second design speaks the script itself.
    assert result.data["voice"]["kind"] == "recording" and stock == []
    assert [design["text"] for design in eleven.designs] == [_sample(), _script()]
    assert result.data["elevenlabs_units"] == len(_sample()) + len(_script())


def test_a_voice_that_gave_way_is_reported_whichever_step_speaks(eleven, stock, cutter, tmp_path):
    eleven.add_voice("The owner's own narrator", age_seconds=9_000_000, category="professional")
    eleven.add_voice("om-server-job_theirs", age_seconds=8_000_000)
    older = eleven.add_voice("om-test-job_older", age_seconds=7200)
    eleven.save_errors = [_error(500, "internal_error", "Something went wrong.")]
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.success and result.data["voice"]["kind"] == "recording"
    assert older not in eleven.voices and result.data["made_room"] == "job_older"


# -- the price before the call ---------------------------------------------------------------

def test_the_quote_is_the_design_plus_the_lines_and_a_later_call_only_its_lines(eleven, tmp_path):
    tool = VoiceoverTTS()
    project = _project(tmp_path)
    first = {**FIRST, "lines": _lines(project)}
    assert tool.estimate_cost(first) == round(usd(len(_sample()) + math.ceil(sum(len(t) for t in TEXTS.values()) * 0.115)), 4)
    assert _budget_quote(tool, first) == (tool.estimate_cost(first), False), "the desk can price it"
    later = {"lines": _lines(project, "B")}
    assert tool.estimate_cost(later) == round(usd(math.ceil(len(TEXTS["B"]) * 0.115)), 4)
    assert tool.estimate_cost({**first, "model": "v4-designed"}) > tool.estimate_cost(first)
    assert tool.estimate_cost({**first, "model": "stock"}) == round(sum(len(t) for t in TEXTS.values()) * 0.0001, 4)


def test_the_dollar_rate_is_a_server_setting(eleven, stock, tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_USD_PER_1K_UNITS", "0.2")
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.cost_usd == round(usd(result.data["elevenlabs_units"], rate=0.2), 4)


# -- small rules -----------------------------------------------------------------------------

def test_what_is_asked_wrongly_is_refused_before_any_call(eleven, stock, tmp_path):
    project = _project(tmp_path)
    tool = VoiceoverTTS()
    wav = [{"id": "A", "text": "Hi there.", "output_path": str(project / "A.wav")}]
    twice = _lines(project, "A") + [{"id": "A2", "text": "Again.", "output_path": _lines(project, "A")[0]["output_path"]}]
    for inputs, words in [({}, "lines is required"), ({"lines": []}, "lines is required"),
                          ({"lines": [{"id": "A", "text": "", "output_path": "a.mp3"}]}, "needs an id, a text"),
                          ({"lines": wav}, "must end in .mp3"), ({"lines": twice}, "share the output_path"),
                          ({**FIRST, "lines": _lines(project), "model": "v5"}, "model must be one of"),
                          ({"voice_description": "A voice.", "gender": "robot", "age": "60s", "lines": _lines(project)}, "no voice on record")]:
        result = tool.execute(inputs)
        assert not result.success and words in result.error, (inputs, result.error)
    assert eleven.paths("POST") == [] and stock == []


@pytest.mark.parametrize("age, band", [("early 20s", "young"), ("mid 20s", "young"), ("early 30s", "young"),
                                       ("late 30s", "middle"), ("mid 40s", "middle"), ("early 50s", "middle"),
                                       ("60s", "old"), ("45 years old", "middle"), ("72", "old"), ("19", "young"),
                                       ("mid sixties", "old"), ("early thirties", "young"), ("late thirties", "middle"),
                                       ("forty", "middle"), ("in her twenties", "young"),
                                       ("middle-aged", "middle"), ("young", "young"), ("elderly", "old"), ("", None)])
def test_an_age_becomes_one_of_three_bands(age, band):
    assert VoiceoverTTS._age_band(age) == band


def test_the_stock_voice_matches_gender_and_age_and_stays_the_same_for_a_video():
    pick = VoiceoverTTS._stock_voice
    assert pick("man", "old", "A warm voice.", "job_1") == "Bill"
    assert pick("woman", "middle", "A warm voice.", "job_1") == "Matilda"
    assert pick("woman", "young", "x", "job_1") in ("Jessica", "Sarah", "Laura")
    assert pick("man", "young", "x", "job_1") in ("Liam", "Will")
    assert pick("man", "middle", "x", "job_1") in ("Brian", "Chris", "Eric")
    assert pick("man", "middle", "x", "job_7") == pick("man", "middle", "another description", "job_7")
    # Only a description that asks for it gets a British voice.
    assert pick("woman", "young", "A woman from London with a British accent.", "job_1") == "Alice"
    assert pick("man", "old", "Grew up in Manchester, England.", "job_1") == "George"
    assert VoiceoverTTS._gender("Woman") == "woman" and VoiceoverTTS._gender("male") == "man"
    assert VoiceoverTTS._gender("robot") is None


def test_the_sample_text_is_the_script_made_to_fit_what_the_design_takes():
    def sample_of(*texts: str) -> str:
        return VoiceoverTTS._sample_text([{"id": str(n), "text": text, "output_path": f"{n}.mp3"}
                                          for n, text in enumerate(texts)])

    # A script shorter than ElevenLabs takes is said again until it is long enough.
    assert sample_of("Fresh bread every morning.") == " ".join(["Fresh bread every morning."] * 4)
    # A long script gives only its start: every character of the sample is paid for.
    sentence = "This sentence is forty-five characters long."
    assert sample_of(*[sentence] * 40) == " ".join([sentence] * 3)
    assert sample_of(*TEXTS.values()) == _sample()
    # No sentence ends in reach: the last whole word that fits.
    words = sample_of(" ".join(["word"] * 200))
    assert 100 <= len(words) <= 240 and words.endswith("word") and not words.endswith(" ")
    # A language written without spaces between sentences ends at its own full stop.
    chinese = sample_of("这是我们今天要介绍的全新咖啡机，它真的非常好用。" * 12)
    assert 100 <= len(chinese) <= 240 and chinese.endswith("。")
    assert all(100 <= len(sample_of(text)) <= 240 for text in ("Hi.", "x" * 99, "x" * 5000, "Go! " * 80))


def test_the_project_is_told_by_the_desk_or_read_from_the_paths(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.delenv("OPENMONTAGE_PROJECT_DIR", raising=False)
    assert VoiceoverTTS._project_dir({}, _lines(project)) == project
    assert VoiceoverTTS._project_dir({"project_dir": str(tmp_path / "given")}, _lines(project)) == tmp_path / "given"
    # Inside a job the desk says which video it is, and a call cannot name another one.
    monkeypatch.setenv("OPENMONTAGE_PROJECT_DIR", str(tmp_path / "from_the_desk"))
    assert VoiceoverTTS._project_dir({}, _lines(project)) == tmp_path / "from_the_desk"
    assert VoiceoverTTS._project_dir({"project_dir": str(tmp_path / "given")}, _lines(project)) == tmp_path / "from_the_desk"


def test_a_job_cannot_reach_another_videos_voice(eleven, stock, tmp_path, monkeypatch):
    mine, theirs = _project(tmp_path, "job_mine"), _project(tmp_path, "job_theirs")
    tool = VoiceoverTTS()
    assert tool.execute({**FIRST, "lines": _lines(theirs)}).success
    their_voice = next(iter(eleven.voices))
    monkeypatch.setenv("OPENMONTAGE_PROJECT_DIR", str(mine))
    # Naming the other project neither speaks with its voice nor replaces it.
    result = tool.execute({**FIRST, "lines": _lines(mine), "project_dir": str(theirs), "new_voice": True})
    assert result.success and their_voice in eleven.voices and len(eleven.voices) == 2
    assert result.data["voice"]["voice_id"] != their_voice
    assert json.loads((mine / "artifacts" / "voice.json").read_text())["voice_name"] == "om-test-job_mine"


def test_it_is_there_with_either_key_and_is_not_a_single_line_tts(monkeypatch):
    tool = VoiceoverTTS()
    for name in ("ELEVENLABS_VOICE_API_KEY", "ELEVENLABS_API_KEY", "FAL_KEY", "FAL_AI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert tool.get_status() == ToolStatus.UNAVAILABLE
    monkeypatch.setenv("FAL_KEY", "x")
    assert tool.get_status() == ToolStatus.AVAILABLE
    monkeypatch.delenv("FAL_KEY")
    monkeypatch.setenv("ELEVENLABS_VOICE_API_KEY", "y")
    assert tool.get_status() == ToolStatus.AVAILABLE
    # The progress bar reads the voice stage from the name; the single-line selector must not route here.
    assert "tts" in tool.name and tool.capability != "tts"
    assert tool.input_schema["properties"]["model"]["default"] == "v4-turbo-designed"
