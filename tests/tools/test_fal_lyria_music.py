from __future__ import annotations

from unittest.mock import MagicMock, patch

from tools.audio.fal_lyria_music import FalLyriaMusic
from tools.base_tool import ToolStatus, _budget_quote
from tools.tool_registry import ToolRegistry


def _response(*, json_data=None, content=b"", status_code=200, text=""):
    response = MagicMock()
    response.json.return_value = json_data
    response.content = content
    response.status_code = status_code
    response.text = text
    response.raise_for_status.return_value = None
    return response


def _queued():
    return _response(json_data={"status_url": "https://queue.example/status",
                                "response_url": "https://queue.example/result",
                                "request_id": "01a1c718-lyria"})


def _tool(monkeypatch, seconds=59.8):
    monkeypatch.setenv("FAL_KEY", "test-key")
    tool = FalLyriaMusic()
    tool._POLL_INTERVAL_SECONDS = 0
    monkeypatch.setattr(FalLyriaMusic, "_probe_seconds", staticmethod(lambda path: seconds))
    return tool


def test_contract_and_flat_price(monkeypatch):
    """fal bills a Lyria 3.5 generation $0.10 whatever its length (three tracks of about a
    minute, 2026-11-10), against $0.60 per started minute for the ElevenLabs tool."""
    tool = FalLyriaMusic()

    monkeypatch.setenv("FAL_KEY", "test-key")
    assert tool.get_status() == ToolStatus.AVAILABLE
    assert tool.estimate_cost({"duration_seconds": 15}) == 0.10
    assert tool.estimate_cost({"duration_seconds": 62}) == 0.10
    assert tool.estimate_cost({}) == 0.10
    # A job's budget check can always price it: an unpriced paid call is refused at the desk.
    assert _budget_quote(tool, {"prompt": "warm lo-fi", "duration_seconds": 30}) == (0.10, False)
    assert tool.get_info()["capability"] == "music_generation"
    assert tool.get_info()["provider"] == "fal.ai"
    assert tool.supports["exact_duration"] is False


def test_registry_discovers_provider(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "test-key")
    registry = ToolRegistry()
    registry.discover()

    tool = registry.get("fal_lyria_music")
    assert tool is not None
    assert tool.get_status() == ToolStatus.AVAILABLE


def test_the_prompt_asks_for_more_than_the_video_needs():
    """The model has no length field and returns about a minute even when asked for 30 seconds,
    so the tool asks for the model's own minute, or more when the video is longer, and the mix
    cuts the track at the video's end."""
    for needed, wish in [(10, "A one-minute track."), (32, "A one-minute track."), (50, "A one-minute track."),
                         (51, "A 90-second track."), (62, "A 90-second track."), (80, "A 90-second track."),
                         (85, "A 2-minute track."), (110, "A 2-minute track."), (130, "A 150-second track.")]:
        assert FalLyriaMusic._length_wish(needed) == wish, needed
    prompt = FalLyriaMusic._model_prompt("Warm lo-fi hip hop, 86 bpm.", 32, True)
    assert prompt.startswith("Warm lo-fi hip hop, 86 bpm. Instrumental only: no lead or backing vocals")
    assert prompt.endswith("A one-minute track.")
    with_voice = FalLyriaMusic._model_prompt("A pop song with a female lead singing 'Rise up'.", 62, False)
    assert "Instrumental only" not in with_voice and with_voice.endswith("A 90-second track.")


def test_an_instrumental_is_told_from_a_song_by_what_the_model_returns():
    # An instrumental's `lyrics` holds section markers alone; a song's holds words and timings.
    assert FalLyriaMusic._sung_words("[[A0]]\n[[B1]]\n[[C2]]\n[[D3]]\n[[E4]]") == ""
    assert FalLyriaMusic._sung_words(None) == "" and FalLyriaMusic._sung_words("") == ""
    assert FalLyriaMusic._sung_words(
        "[[A0]]\n[[B1]]\n[12.0:] Dust motes dancing in the light,\n[:] Morning breaks so soft and white."
    ) == "Dust motes dancing in the light, Morning breaks so soft and white."


def test_execute_submits_once_and_reports_the_real_length(tmp_path, monkeypatch):
    output_path = tmp_path / "music.mp3"
    tool = _tool(monkeypatch, seconds=59.79)
    result_response = _response(json_data={"audio": {"url": "https://media.example/music.mp3"},
                                           "lyrics": "[[A0]]\n[[B1]]\n[[C2]]"})
    with (
        patch("requests.post", return_value=_queued()) as mock_post,
        patch("requests.get", side_effect=[_response(json_data={"status": "COMPLETED"}), result_response,
                                           _response(content=b"fake-mp3")]) as mock_get,
    ):
        result = tool.execute({"prompt": "Upbeat pop-electronic bed, 108 bpm.", "duration_seconds": 32,
                               "output_path": str(output_path)})

    assert result.success is True, result.error
    assert result.cost_usd == 0.10
    assert result.model == "google/lyria-3.5"
    assert mock_post.call_count == 1
    assert mock_post.call_args.args == ("https://queue.fal.run/google/lyria-3.5",)
    # The prompt is the model's only input: the description, the instrumental rule, the length wish.
    sent = mock_post.call_args.kwargs["json"]
    assert set(sent) == {"prompt"}
    assert sent["prompt"].startswith("Upbeat pop-electronic bed, 108 bpm. Instrumental only:")
    assert sent["prompt"].endswith("A one-minute track.")
    assert mock_get.call_count == 3
    assert output_path.read_bytes() == b"fake-mp3"
    # The request id goes out with the result: the job's ledger keeps it, and settlement checks
    # the estimate against what fal billed for exactly this request.
    assert result.data["request_ids"] == ["01a1c718-lyria"]
    # The length reported is the file's, not the one asked for: the track is longer than the video.
    assert result.data["duration_seconds"] == 59.79 and result.data["needed_seconds"] == 32.0
    assert result.data["sung_words"] is False and "note" not in result.data
    assert result.data["model_prompt"] == sent["prompt"] and result.data["prompt"] == "Upbeat pop-electronic bed, 108 bpm."


def test_a_short_track_and_an_unwanted_voice_are_said_plainly(tmp_path, monkeypatch):
    tool = _tool(monkeypatch, seconds=58.2)
    result_response = _response(json_data={"audio": {"url": "https://media.example/music.mp3"},
                                           "lyrics": "[[A0]]\n[4.0:] Rise up and shine today"})
    with (
        patch("requests.post", return_value=_queued()),
        patch("requests.get", side_effect=[_response(json_data={"status": "COMPLETED"}), result_response,
                                           _response(content=b"fake-mp3")]),
    ):
        result = tool.execute({"prompt": "Bright pop bed.", "duration_seconds": 62,
                               "output_path": str(tmp_path / "music.mp3")})

    assert result.success is True, result.error  # the track exists and was paid for
    assert result.data["sung_words"] is True
    assert "58.2 s, shorter than the 62 s asked for" in result.data["note"]
    assert "sung words although an instrumental was asked for" in result.data["note"]


def test_a_refused_prompt_comes_back_in_fals_own_words(tmp_path, monkeypatch):
    tool = _tool(monkeypatch)
    refused = _response(status_code=422, text='{"detail": "content_policy_violation"}')
    with (
        patch("requests.post", return_value=_queued()),
        patch("requests.get", side_effect=[_response(json_data={"status": "COMPLETED"}), refused]),
    ):
        result = tool.execute({"prompt": "x", "duration_seconds": 30, "output_path": str(tmp_path / "m.mp3")})

    assert result.success is False
    assert "422" in result.error and "content_policy_violation" in result.error
    assert not (tmp_path / "m.mp3").exists()


def test_execute_rejects_missing_inputs_without_a_request(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "test-key")
    with patch("requests.post") as mock_post:
        no_length = FalLyriaMusic().execute({"prompt": "gentle piano"})
        no_prompt = FalLyriaMusic().execute({"prompt": "  ", "duration_seconds": 30})
        too_long = FalLyriaMusic().execute({"prompt": "gentle piano", "duration_seconds": 400})

    assert no_length.success is False and no_length.error == "duration_seconds is required"
    assert no_prompt.success is False and no_prompt.error == "prompt is required"
    assert too_long.success is False and "between 3 and 170" in too_long.error
    mock_post.assert_not_called()


def test_error_redacts_fal_key(monkeypatch):
    secret = "test-production-shaped-fal-key"
    monkeypatch.setenv("FAL_KEY", secret)
    with patch("requests.post", side_effect=RuntimeError(f"request used {secret}")):
        result = FalLyriaMusic().execute({"prompt": "gentle piano", "duration_seconds": 20})

    assert result.success is False
    assert secret not in result.error
    assert "[REDACTED]" in result.error
