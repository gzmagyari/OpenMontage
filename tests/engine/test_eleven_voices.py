"""The voices this installation keeps on ElevenLabs: which are ours, which may be removed to make
room, and which go when their video is gone. The account owner's own voices and the voices of
another installation on the same account are never touched."""

from __future__ import annotations

import time

import pytest

from lib import eleven_voices
from tests.tools.eleven_fake import FakeEleven, _Response, _error

KEY = "eleven-test-key"


@pytest.fixture()
def eleven(monkeypatch):
    fake = FakeEleven(slots=30)
    monkeypatch.setattr("requests.request", fake.request)
    monkeypatch.setenv("OPENMONTAGE_INSTANCE_ID", "test")
    return fake


# -- the key ----------------------------------------------------------------------------------

def test_the_voices_have_a_key_name_of_their_own(monkeypatch):
    """Under the general name the same key would also switch on the older ElevenLabs tools
    (single lines, music, soundtracks) for every job, on the allowance the voices live on."""
    for name in eleven_voices.KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    assert eleven_voices.api_key() is None
    monkeypatch.setenv("ELEVENLABS_API_KEY", "general")
    assert eleven_voices.api_key() == "general"
    monkeypatch.setenv("ELEVENLABS_VOICE_API_KEY", "voices")
    assert eleven_voices.api_key() == "voices"
    monkeypatch.setenv("ELEVENLABS_VOICE_API_KEY", "")
    assert eleven_voices.api_key() == "general"
    # The other ElevenLabs tools do not read the voices' key.
    import os

    from tools.audio.elevenlabs_tts import ElevenLabsTTS
    from tools.audio.music_gen import MusicGen
    from tools.base_tool import ToolStatus

    monkeypatch.delenv("ELEVENLABS_API_KEY")
    monkeypatch.setenv("ELEVENLABS_VOICE_API_KEY", "voices")
    assert "ELEVENLABS_API_KEY" not in os.environ
    assert ElevenLabsTTS().get_status() == ToolStatus.UNAVAILABLE
    assert MusicGen().get_status() == ToolStatus.UNAVAILABLE


# -- whose voice is it -----------------------------------------------------------------------

def test_an_installation_gets_its_own_id_once_and_keeps_it(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENMONTAGE_INSTANCE_ID", raising=False)
    first = eleven_voices.ensure_instance_id(tmp_path)
    assert len(first) == 8 and (tmp_path / "instance_id").read_text().strip() == first
    # The tools the provider starts read it from the environment.
    assert eleven_voices.prefix() == f"om-{first}"
    monkeypatch.delenv("OPENMONTAGE_INSTANCE_ID")
    assert eleven_voices.ensure_instance_id(tmp_path) == first, "a restart keeps the id"
    # An operator's own id wins, and the file stays as it was.
    monkeypatch.setenv("OPENMONTAGE_INSTANCE_ID", "hungary")
    assert eleven_voices.ensure_instance_id(tmp_path) == "hungary"
    assert (tmp_path / "instance_id").read_text().strip() == first


def test_an_id_is_still_made_when_the_data_folder_cannot_be_written(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENMONTAGE_INSTANCE_ID", raising=False)
    blocked = tmp_path / "a_file"
    blocked.write_text("not a folder")
    assert len(eleven_voices.ensure_instance_id(blocked / "data")) == 8


def test_a_voice_is_ours_only_by_this_installations_prefix(monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_INSTANCE_ID", "test")
    assert eleven_voices.voice_name("job_abc") == "om-test-job_abc"
    assert eleven_voices.project_of({"name": "om-test-job_abc"}) == "job_abc"
    for name in ("The owner's own narrator", "om-testing-job_abc", "om-server-job_abc", "om-test-", "job_abc", ""):
        assert eleven_voices.project_of({"name": name}) is None, name
    # Whatever an operator writes, the id in a name is letters and digits only, so the "-" after
    # it always ends it.
    monkeypatch.setenv("OPENMONTAGE_INSTANCE_ID", "my server-01!")
    assert eleven_voices.prefix() == "om-myserver01"
    monkeypatch.delenv("OPENMONTAGE_INSTANCE_ID")
    assert eleven_voices.prefix() == "om-local"


def test_a_voices_age_is_our_own_label_first():
    assert eleven_voices.created_at({"labels": {"om_created": "1700000000"}, "created_at_unix": 5}) == 1700000000.0
    assert eleven_voices.created_at({"labels": {}, "created_at_unix": 1700000500}) == 1700000500.0
    assert eleven_voices.created_at({"labels": {"om_created": "soon"}, "created_at_unix": None}) == 0.0
    assert eleven_voices.created_at({}) == 0.0


# -- one request --------------------------------------------------------------------------------

def test_a_call_carries_the_key_and_gives_back_elevenlabs_own_words(monkeypatch):
    seen = {}

    def request(method, url, headers=None, timeout=None, **kwargs):
        seen.update(method=method, url=url, headers=headers, timeout=timeout, kwargs=kwargs)
        return _Response(200, {"ok": True})

    monkeypatch.setattr("requests.request", request)
    assert eleven_voices.call("POST", "/v1/text-to-voice", KEY, timeout=12, json={"a": 1}).json() == {"ok": True}
    assert seen["url"] == "https://api.elevenlabs.io/v1/text-to-voice" and seen["timeout"] == 12
    assert seen["headers"] == {"xi-api-key": KEY, "Content-Type": "application/json"}

    answers = [
        (_error(429, "concurrent_limit_exceeded", "Too many concurrent requests."), 429, "concurrent_limit_exceeded",
         "Too many concurrent requests."),
        (_Response(422, {"detail": [{"type": "string_too_short", "msg": "String should have at least 100 characters"}]}),
         422, None, "String should have at least 100 characters"),
        (_Response(502, None), 502, None, "no details given"),
    ]
    for response, status, code, words in answers:
        monkeypatch.setattr("requests.request", lambda *a, _r=response, **k: _r)
        with pytest.raises(eleven_voices.ElevenError) as caught:
            eleven_voices.call("GET", "/v1/user/subscription", KEY)
        assert (caught.value.status, caught.value.code) == (status, code) and words in str(caught.value)
        assert KEY not in str(caught.value)


def test_a_network_failure_is_an_error_that_names_no_address_or_key(monkeypatch):
    def request(*_args, **_kwargs):
        raise ConnectionError(f"could not reach https://api.elevenlabs.io with {KEY}")

    monkeypatch.setattr("requests.request", request)
    with pytest.raises(eleven_voices.ElevenError) as caught:
        eleven_voices.call("GET", "/v1/user/subscription", KEY)
    assert caught.value.status is None and str(caught.value) == "ElevenLabs could not be reached: ConnectionError"


def test_what_the_account_has_left(eleven):
    eleven.add_voice("The owner's own narrator")
    eleven.add_edit_counter, eleven.character_count = 50, 9_000
    assert eleven_voices.room(eleven_voices.account(KEY)) == {"slots": 29, "creations": 4, "characters": 1_000}
    # A plan that does not say a limit is not treated as used up.
    assert eleven_voices.room({"voice_limit": 3}) == {"slots": None, "creations": None, "characters": None}


# -- our voices ----------------------------------------------------------------------------------

def test_our_voices_are_listed_across_pages_oldest_first(eleven):
    eleven.page_size = 2
    eleven.add_voice("The owner's own narrator", age_seconds=9_000_000, category="professional")
    newest = eleven.add_voice("om-test-job_new", age_seconds=10)
    eleven.add_voice("om-server-job_theirs", age_seconds=500)
    oldest = eleven.add_voice("om-test-job_old", age_seconds=90_000)
    middle = eleven.add_voice("om-test-job_mid", age_seconds=4_000)

    assert [voice["voice_id"] for voice in eleven_voices.ours(KEY)] == [oldest, middle, newest]
    assert eleven.count("GET", "/v2/voices") == 3, "five voices, two a page"
    assert eleven_voices.find(KEY, "job_mid")["voice_id"] == middle
    assert eleven_voices.find(KEY, "job_theirs") is None and eleven_voices.find(KEY, "job_none") is None


def test_removing_a_voice_that_is_already_gone_counts_as_removed(eleven, monkeypatch):
    voice = eleven.add_voice("om-test-job_a")
    assert eleven_voices.remove(KEY, voice) is True and voice not in eleven.voices
    assert eleven_voices.remove(KEY, voice) is True
    monkeypatch.setattr("requests.request", lambda *a, **k: _error(500, "internal_error", "Something went wrong."))
    assert eleven_voices.remove(KEY, "voice-x") is False


def test_room_is_made_with_the_oldest_voice_of_ours_that_is_not_busy(eleven):
    owner = eleven.add_voice("The owner's own narrator", age_seconds=9_000_000, category="professional")
    theirs = eleven.add_voice("om-server-job_theirs", age_seconds=8_000_000)
    old = eleven.add_voice("om-test-job_old", age_seconds=90_000)
    older = eleven.add_voice("om-test-job_older", age_seconds=200_000)
    busy = eleven.add_voice("om-test-job_busy", age_seconds=eleven_voices.BUSY_SECONDS - 30)

    assert eleven_voices.make_room(KEY)["voice_id"] == older
    assert eleven_voices.make_room(KEY)["voice_id"] == old
    assert eleven_voices.make_room(KEY) is None, "a voice that may be speaking is never removed"
    assert set(eleven.voices) == {owner, theirs, busy}
    # ...until it is old enough.
    assert eleven_voices.make_room(KEY, now=time.time() + 60)["voice_id"] == busy
    assert set(eleven.voices) == {owner, theirs}


def test_a_voice_that_cannot_be_removed_does_not_stop_the_next_one(eleven, monkeypatch):
    stuck = eleven.add_voice("om-test-job_stuck", age_seconds=200_000)
    free = eleven.add_voice("om-test-job_free", age_seconds=90_000)

    def request(method, url, **kwargs):
        if method == "DELETE" and url.endswith(stuck):
            return _error(500, "internal_error", "Something went wrong.")
        return eleven.request(method, url, **kwargs)

    monkeypatch.setattr("requests.request", request)
    assert eleven_voices.make_room(KEY)["voice_id"] == free and stuck in eleven.voices


def test_the_sweep_removes_the_voices_of_videos_that_are_gone_and_only_those(eleven):
    owner = eleven.add_voice("The owner's own narrator", age_seconds=9_000_000, category="professional")
    theirs = eleven.add_voice("om-server-job_gone", age_seconds=8_000_000)
    kept = eleven.add_voice("om-test-job_kept", age_seconds=200_000)
    gone = eleven.add_voice("om-test-job_gone", age_seconds=300_000)
    also_gone = eleven.add_voice("om-test-job_gone2", age_seconds=100_000)
    just_made = eleven.add_voice("om-test-job_new", age_seconds=60)  # its video may not be on record yet

    removed = eleven_voices.sweep(KEY, iter(["job_kept", "job_other"]))
    assert sorted(removed) == ["job_gone", "job_gone2"]
    assert set(eleven.voices) == {owner, theirs, kept, just_made}
    assert gone not in eleven.voices and also_gone not in eleven.voices
    assert eleven_voices.sweep(KEY, ["job_kept"]) == [], "a second round finds nothing more"
