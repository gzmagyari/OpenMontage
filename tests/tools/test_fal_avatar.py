from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools.avatar.fal_avatar import FalAvatar
from tools.base_tool import ToolStatus
from tools.provider_pricing import PriceQuoteRequired
from tools.tool_registry import ToolRegistry


def _response(*, json_data=None, content=b"", status_code=200, text=""):
    response = MagicMock()
    response.json.return_value = json_data
    response.content = content
    response.status_code = status_code
    response.text = text
    response.raise_for_status.return_value = None
    return response


def test_contract_and_cost(monkeypatch):
    tool = FalAvatar()

    monkeypatch.setenv("FAL_KEY", "test-key")
    assert tool.get_status() == ToolStatus.AVAILABLE
    assert tool.get_info()["capability"] == "avatar"
    assert tool.get_info()["provider"] == "fal.ai"
    assert tool.estimate_cost({"model": "kling-v2-standard", "audio_duration_seconds": 10}) == 0.562
    assert tool.estimate_cost({"model": "omnihuman-1.5", "audio_duration_seconds": 10}) == 1.60
    # Real billed rates (2026-11-09): plate $0.056/s at 768P, lip-sync pass $0.20/s, whole seconds.
    # The plate is asked for the line plus 0.6 s of headroom and billed for that: 11 s for a 10 s
    # line (video 714's billing events); the lip-sync pass follows the line, 10 s.
    assert tool.estimate_cost({"model": "h3max-acting", "audio_duration_seconds": 10}) == 2.616
    assert tool.estimate_cost({"model": "h3max", "audio_duration_seconds": 10}) == 0.616
    assert tool.estimate_cost({"audio_duration_seconds": 10}) == 0.616  # native is the default
    assert tool.estimate_cost({"video_url": "https://x/v.mp4", "audio_duration_seconds": 10}) == 2.00
    with pytest.raises(PriceQuoteRequired):
        tool.estimate_cost({"model": "veed-fabric-1.0"})  # no audio length to bill against


def test_registry_discovers_provider(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "test-key")
    registry = ToolRegistry()
    registry.discover()

    tool = registry.get("fal_avatar")
    assert tool is not None
    assert tool.get_status() == ToolStatus.AVAILABLE


def test_execute_submits_once_and_downloads_video(tmp_path, monkeypatch):
    monkeypatch.setenv("FAL_KEY", "test-key")
    output_path = tmp_path / "avatar.mp4"
    tool = FalAvatar()
    tool._POLL_INTERVAL_SECONDS = 0

    post_response = _response(
        json_data={
            "status_url": "https://queue.fal.run/fal-ai/kling-video/requests/r1/status",
            "response_url": "https://queue.fal.run/fal-ai/kling-video/requests/r1",
        }
    )
    status_response = _response(json_data={"status": "COMPLETED"})
    result_response = _response(json_data={"video": {"url": "https://media.example/avatar.mp4"}, "duration": 10.0})
    video_response = _response(content=b"fake-mp4")

    with (
        patch("requests.post", return_value=post_response) as mock_post,
        patch("requests.get", side_effect=[status_response, result_response, video_response]) as mock_get,
    ):
        result = tool.execute(
            {
                "image_url": "https://media.example/face.jpg",
                "audio_url": "https://media.example/line.mp3",
                "model": "kling-v2-standard",
                "prompt": "subtle natural selfie-video energy",
                "output_path": str(output_path),
            }
        )

    assert result.success is True, result.error
    assert result.cost_usd == 0.562  # 10 s x $0.0562/s from the provider-reported duration
    assert result.model == "fal-ai/kling-video/ai-avatar/v2/standard"
    assert output_path.read_bytes() == b"fake-mp4"
    assert mock_post.call_count == 1
    assert mock_post.call_args.args == ("https://queue.fal.run/fal-ai/kling-video/ai-avatar/v2/standard",)
    assert mock_post.call_args.kwargs["json"] == {
        "image_url": "https://media.example/face.jpg",
        "audio_url": "https://media.example/line.mp3",
        "prompt": "subtle natural selfie-video energy",
    }
    assert mock_get.call_count == 3


def test_fabric_requires_resolution_default(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "test-key")
    tool = FalAvatar()
    tool._POLL_INTERVAL_SECONDS = 0
    post_response = _response(
        json_data={
            "status_url": "https://queue.fal.run/veed/requests/r2/status",
            "response_url": "https://queue.fal.run/veed/requests/r2",
        }
    )
    failed = _response(json_data={"status": "FAILED"})
    with patch("requests.post", return_value=post_response) as mock_post, patch("requests.get", return_value=failed):
        result = tool.execute(
            {"image_url": "https://x/face.jpg", "audio_url": "https://x/line.mp3", "model": "veed-fabric-1.0"}
        )
    assert result.success is False
    assert mock_post.call_args.kwargs["json"]["resolution"] == "720p"
    assert result.data["resume_job"]["model"] == "veed-fabric-1.0"  # resumable after a transient failure


def test_execute_rejects_missing_inputs(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "test-key")
    with patch("requests.post") as mock_post:
        result = FalAvatar().execute({"image_url": "https://x/face.jpg"})

    assert result.success is False
    assert result.error == "audio_url or audio_path is required"
    mock_post.assert_not_called()


def test_error_redacts_fal_key(monkeypatch):
    secret = "test-production-shaped-fal-key"
    monkeypatch.setenv("FAL_KEY", secret)
    with patch("requests.post", side_effect=RuntimeError(f"request used {secret}")):
        result = FalAvatar().execute(
            {"image_url": "https://x/face.jpg", "audio_url": "https://x/line.mp3",
             "model": "kling-v2-standard"}
        )

    assert result.success is False
    assert secret not in result.error
    assert "[REDACTED]" in result.error


def _chain_tool(monkeypatch, tmp_path, peak=0.8, audio_seconds=9.0):
    """A FalAvatar with the local-media steps stubbed (no ffmpeg/ffprobe needed)."""
    monkeypatch.setenv("FAL_KEY", "test-key")
    tool = FalAvatar()
    tool._POLL_INTERVAL_SECONDS = 0
    monkeypatch.setattr(FalAvatar, "_audio_peak", staticmethod(lambda path: peak))
    monkeypatch.setattr(FalAvatar, "_probe_duration", staticmethod(lambda path: audio_seconds))
    mux_calls = []
    monkeypatch.setattr(FalAvatar, "_mux_audio", staticmethod(
        lambda video, audio, dst, trim_seconds=None:
        mux_calls.append(trim_seconds) or dst.write_bytes(video.read_bytes()) or dst))
    tool._test_mux_calls = mux_calls
    monkeypatch.setattr(FalAvatar, "_upload", staticmethod(lambda path, key: f"https://up.example/{path.name}"))
    audio = tmp_path / "line.wav"
    audio.write_bytes(b"RIFF-fake")
    return tool, audio


def test_composite_runs_plate_then_sync_and_remuxes(tmp_path, monkeypatch):
    tool, audio = _chain_tool(monkeypatch, tmp_path)
    out = tmp_path / "take.mp4"

    def submit(url, **kw):
        n = url.count("lip-sync")
        return _response(json_data={
            "status_url": f"https://queue.fal.run/x/requests/r{n}/status",
            "response_url": f"https://queue.fal.run/x/requests/r{n}",
        })

    results = iter([
        _response(json_data={"status": "COMPLETED"}),
        _response(json_data={"video": {"url": "https://media.example/plate.mp4"}, "duration": 10.0}),
        _response(json_data={"status": "COMPLETED"}),
        _response(json_data={"video": {"url": "https://media.example/synced.mp4"}, "duration": 9.0}),
        _response(content=b"fake-synced-mp4"),
    ])
    with (
        patch("requests.post", side_effect=submit) as mock_post,
        patch("requests.get", side_effect=lambda *a, **k: next(results)),
    ):
        result = tool.execute({
            "image_path": str(tmp_path / "face.jpg"),
            "audio_path": str(audio),
            "model": "h3max-acting",  # the composite is opt-in now; native is the default
            "prompt": "she leans toward the lens and talks straight into the camera",
            "output_path": str(out),
        })

    assert result.success is True, result.error
    assert out.read_bytes() == b"fake-synced-mp4"  # the stubbed mux copies the synced video through
    assert tool._test_mux_calls == [None]  # the sync output is already exact: no trim
    assert mock_post.call_count == 2
    plate_payload = mock_post.call_args_list[0].kwargs["json"]
    assert plate_payload["target_audio_url"] == "https://up.example/line.wav"
    assert plate_payload["duration"] == 10  # ceil(9.0 + 0.6)
    sync_payload = mock_post.call_args_list[1].kwargs["json"]
    assert sync_payload == {"video_url": "https://media.example/plate.mp4",
                            "audio_url": "https://up.example/line.wav",
                            "duration_mode": "cut_off", "resolution": "768P"}
    assert result.cost_usd == round(0.056 * 10 + 0.20 * 9, 4)
    assert result.data["cost_breakdown"] == {"plate": 0.56, "sync": 1.8}


def test_fix_mode_syncs_an_existing_video(tmp_path, monkeypatch):
    tool, audio = _chain_tool(monkeypatch, tmp_path)
    out = tmp_path / "fixed.mp4"
    post = _response(json_data={"status_url": "https://queue.fal.run/x/requests/r/status",
                                "response_url": "https://queue.fal.run/x/requests/r"})
    results = iter([
        _response(json_data={"status": "COMPLETED"}),
        _response(json_data={"video": {"url": "https://media.example/synced.mp4"}, "duration": 9.0}),
        _response(content=b"fake-synced-mp4"),
    ])
    with (
        patch("requests.post", return_value=post) as mock_post,
        patch("requests.get", side_effect=lambda *a, **k: next(results)),
    ):
        result = tool.execute({"video_url": "https://x/acted.mp4", "audio_path": str(audio),
                               "output_path": str(out)})

    assert result.success is True, result.error
    assert mock_post.call_count == 1  # no plate: only the lip-sync pass
    assert mock_post.call_args.kwargs["json"]["video_url"] == "https://x/acted.mp4"
    assert result.cost_usd == 1.8
    assert result.data["mode"] == "sync"


def test_composite_refuses_silent_audio(tmp_path, monkeypatch):
    tool, audio = _chain_tool(monkeypatch, tmp_path, peak=0.0001)
    with patch("requests.post") as mock_post:
        result = tool.execute({"image_path": str(tmp_path / "face.jpg"), "audio_path": str(audio),
                               "output_path": str(tmp_path / "take.mp4")})

    assert result.success is False
    assert "silent" in result.error
    mock_post.assert_not_called()  # nothing was spent


def test_native_runs_the_plate_alone_and_trims_to_the_line(tmp_path, monkeypatch):
    tool, audio = _chain_tool(monkeypatch, tmp_path)
    out = tmp_path / "take.mp4"
    post = _response(json_data={"status_url": "https://queue.fal.run/x/requests/rp/status",
                                "response_url": "https://queue.fal.run/x/requests/rp",
                                "request_id": "rid-plate"})
    results = iter([
        _response(json_data={"status": "COMPLETED"}),
        _response(json_data={"video": {"url": "https://media.example/plate.mp4"}, "duration": 10.0}),
        _response(content=b"fake-plate-mp4"),
    ])
    with (
        patch("requests.post", return_value=post) as mock_post,
        patch("requests.get", side_effect=lambda *a, **k: next(results)),
    ):
        result = tool.execute({"image_path": str(tmp_path / "face.jpg"), "audio_path": str(audio),
                               "output_path": str(out)})  # no model: native is the default

    assert result.success is True, result.error
    assert mock_post.call_count == 1  # the plate alone: no lip-sync pass is bought
    assert "lip-sync" not in mock_post.call_args.args[0]
    assert out.read_bytes() == b"fake-plate-mp4"
    assert tool._test_mux_calls == [9.0]  # trimmed to the line, not the plate's headroom
    assert result.cost_usd == round(0.056 * 10, 4)  # 10s plate at the 768P rate, nothing else
    assert result.data["mode"] == "native" and result.data["model"] == "h3max"
    assert result.data["endpoints"] == ["minimax/h3-max/image-to-video"]
    assert result.data["request_ids"] == ["rid-plate"]



def test_a_take_is_recorded_at_the_plate_length_it_asked_for(tmp_path, monkeypatch):
    """fal's answer for a plate carries no duration, so the recorded cost fell back to the line:
    video 714's 5.54 s take reached the ledger at 6 seconds ($0.336) and was billed 7 ($0.392)."""
    tool, audio = _chain_tool(monkeypatch, tmp_path, audio_seconds=5.542)
    out = tmp_path / "take.mp4"
    post = _response(json_data={"status_url": "https://queue.fal.run/x/requests/rp/status",
                                "response_url": "https://queue.fal.run/x/requests/rp",
                                "request_id": "rid-plate"})
    results = iter([
        _response(json_data={"status": "COMPLETED"}),
        _response(json_data={"video": {"url": "https://media.example/plate.mp4"}}),  # no duration
        _response(content=b"fake-plate-mp4"),
    ])
    inputs = {"image_path": str(tmp_path / "face.jpg"), "audio_path": str(audio), "output_path": str(out)}
    assert tool.estimate_cost(inputs) == 0.392  # the quote the budget is held to says the same
    with (
        patch("requests.post", return_value=post) as mock_post,
        patch("requests.get", side_effect=lambda *a, **k: next(results)),
    ):
        result = tool.execute(inputs)

    assert result.success is True, result.error
    assert mock_post.call_args.kwargs["json"]["duration"] == 7  # ceil(5.542 + 0.6)
    assert result.cost_usd == 0.392
    assert result.data["cost_breakdown"] == {"plate": 0.392}
    assert result.data["resume_job"]["plate_seconds"] == 7
    assert tool._test_mux_calls == [5.542]  # the delivered take is still cut to the line
