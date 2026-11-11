"""voiceover_tts: one voice for one video. A voice is designed for the presenter, saved, used for
every line and kept for the video; a later call speaks with it again; and when ElevenLabs cannot
give a voice the lines are spoken by a stock voice instead, never by two voices."""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import pytest

from tests.tools.eleven_fake import FakeEleven, _error
from tools.audio import voiceover_tts
from tools.audio.fal_elevenlabs_tts import FalElevenLabsTTS
from tools.audio.voiceover_tts import GONE, VoiceoverTTS
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
    for name in ("ELEVENLABS_USD_PER_1K_UNITS", "OPENMONTAGE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    return fake


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


@pytest.mark.parametrize("setup, reason", [
    (lambda fake: setattr(fake, "add_edit_counter", fake.max_add_edits), "voice creations on the ElevenLabs account are used up"),
    (lambda fake: setattr(fake, "character_count", fake.character_limit - 50), "too few characters left"),
])
def test_a_used_up_account_is_seen_before_anything_is_spent(eleven, stock, tmp_path, setup, reason):
    setup(eleven)
    before = eleven.character_count
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})
    assert result.success and reason in result.data["fallback"]
    assert eleven.paths("POST") == [], "no design was bought that could not be saved"
    assert eleven.character_count == before and result.data["elevenlabs_units"] == 0
    assert len(stock) == 3 and result.data["request_ids"]


def test_a_refused_description_falls_back_with_elevenlabs_own_words(eleven, stock, tmp_path):
    eleven.design_error = _error(400, "voice_design_refused", "The description names a real person.")
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})
    assert result.success and "The description names a real person." in result.data["fallback"]
    assert eleven.voices == {} and len(stock) == 3 and result.data["elevenlabs_units"] == 0


def test_when_speaking_fails_midway_every_line_is_spoken_again_by_the_stock_voice(eleven, stock, tmp_path, no_waiting):
    """Two voices in one video is the one thing that must not happen."""
    project = _project(tmp_path)
    eleven.speech_troubles = [0, 500, 500, 500, 500]  # the first line works, then ElevenLabs breaks
    eleven.speech_always = 500
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})

    assert result.success, result.error
    assert sorted(call["text"] for call in stock) == sorted(TEXTS[i] for i in "ABC")
    for line in _lines(project):
        assert Path(line["output_path"]).read_bytes().startswith(b"STOCK[Bill]"), "no line kept the designed voice"
    assert eleven.voices == {}, "a voice that could not be used is not kept"
    assert _record(project)["kind"] == "stock"
    # Both were charged, so the cost is the sum and stays the tool's own figure.
    spoken = len(eleven.speech)
    assert spoken >= 1 and result.data["elevenlabs_units"] > len(_sample())
    assert result.data["costs"]["stock_usd"] > 0 and result.data["request_ids"] == []
    assert result.cost_usd == round(usd(result.data["elevenlabs_units"]) + result.data["costs"]["stock_usd"], 4)


def test_lines_still_waiting_are_not_spoken_in_a_voice_that_will_not_be_used(eleven, stock, tmp_path, no_waiting):
    project = _project(tmp_path)
    TEXTS.update({str(n): f"Line number {n} of a longer script that needs speaking." for n in range(6)})
    try:
        eleven.speech_troubles = [0]
        eleven.speech_always = 500
        result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project, "ABC012345")})
    finally:
        for n in range(6):
            TEXTS.pop(str(n))
    assert result.success and result.data["voice"]["kind"] == "stock" and len(stock) == 9
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


def test_the_designed_path_has_a_time_limit(eleven, stock, tmp_path, no_waiting, monkeypatch):
    monkeypatch.setattr(VoiceoverTTS, "_DESIGNED_SECONDS", 0.0)
    eleven.speech_always = 429
    project = _project(tmp_path)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(project)})
    assert result.success and "in time" in result.data["fallback"] and len(stock) == 3 and eleven.voices == {}


def test_the_stock_voice_failing_too_is_a_failure_that_keeps_what_was_spent(eleven, tmp_path, monkeypatch):
    monkeypatch.setattr(FalElevenLabsTTS, "execute", lambda self, inputs: ToolResult(success=False, error="fal is down"))
    eleven.design_error = _error(500, "internal_error", "Something went wrong.")
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert not result.success and "stock voice could not speak" in result.error and "fal is down" in result.error


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


def test_a_voice_that_may_be_speaking_is_never_removed_to_make_room(eleven, stock, tmp_path):
    for n in range(3):
        eleven.add_voice(f"om-test-job_busy{n}", age_seconds=60)  # saved a minute ago
    before = set(eleven.voices)
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.success and "no voice slot is free" in result.data["fallback"]
    assert set(eleven.voices) == before and len(stock) == 3
    assert eleven.count("POST", "/v1/text-to-voice/design") == 0, "nothing was designed that had no place"


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


def test_the_no_slot_answer_alone_is_enough_to_make_room(eleven, stock, tmp_path):
    """The answer to the save is what is acted on, so a count of saved voices that lagged would
    not send a video to the stock voice."""
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
    assert len(eleven.voices) == 2 and len(stock) == 3


def test_a_save_refused_for_another_reason_removes_no_voice(eleven, stock, tmp_path):
    old = eleven.add_voice("om-test-job_older", age_seconds=7200)
    eleven.save_errors = [_error(500, "internal_error", "Something went wrong.")]
    result = VoiceoverTTS().execute({**FIRST, "lines": _lines(_project(tmp_path))})
    assert result.success and "Something went wrong." in result.data["fallback"] and len(stock) == 3
    assert list(eleven.voices) == [old], "an older video keeps its voice when the slot was not the problem"


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
