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
            {"image_url": "https://x/face.jpg", "audio_url": "https://x/line.mp3"}
        )

    assert result.success is False
    assert secret not in result.error
    assert "[REDACTED]" in result.error
