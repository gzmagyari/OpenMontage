from __future__ import annotations

import shutil
import subprocess
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
    # The plate is billed for the seconds it is asked for: 10 for a 10 s line, whose clip
    # (243 frames, 10.13 s) holds the line; the lip-sync pass follows the line, 10 s.
    assert tool.estimate_cost({"model": "h3max-acting", "audio_duration_seconds": 10}) == 2.56
    assert tool.estimate_cost({"model": "h3max", "audio_duration_seconds": 10}) == 0.56
    # The default take is H3 Max Turbo, at half the plate rate ($0.028/s at 768P, $0.0175 at 480P).
    assert tool.estimate_cost({"audio_duration_seconds": 10}) == 0.28
    assert tool.estimate_cost({"model": "h3max-turbo", "audio_duration_seconds": 10}) == 0.28
    assert tool.estimate_cost({"audio_duration_seconds": 10, "resolution": "480P"}) == 0.175
    with pytest.raises(ValueError, match="h3max-turbo, h3max, h3max-acting"):
        tool.estimate_cost({"model": "no-such-model", "audio_duration_seconds": 10})
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


def _chain_tool(monkeypatch, tmp_path, peak=0.8, audio_seconds=9.0, clip_seconds=None):
    """A FalAvatar with the local-media steps stubbed (no ffmpeg/ffprobe needed).
    ``clip_seconds``: the length of the clip the model is taken to have returned (None: unreadable)."""
    monkeypatch.setenv("FAL_KEY", "test-key")
    tool = FalAvatar()
    tool._POLL_INTERVAL_SECONDS = 0
    monkeypatch.setattr(FalAvatar, "_audio_peak", staticmethod(lambda path: peak))
    monkeypatch.setattr(FalAvatar, "_probe_duration", staticmethod(lambda path: audio_seconds))
    monkeypatch.setattr(FalAvatar, "_clip_seconds", staticmethod(lambda path: clip_seconds))
    mux_calls, mux_speeds, pad_calls = [], [], []
    monkeypatch.setattr(FalAvatar, "_mux_audio", staticmethod(
        lambda video, audio, dst, trim_seconds=None, speed=None:
        mux_calls.append(trim_seconds) or mux_speeds.append(speed)
        or dst.write_bytes(video.read_bytes()) or dst))
    monkeypatch.setattr(FalAvatar, "_pad_audio", staticmethod(
        lambda src, dst, seconds:
        (pad_calls.append((src.name, round(seconds, 3))), dst.write_bytes(b"RIFF-padded"), dst)[-1]))
    tool._test_mux_calls = mux_calls
    tool._test_mux_speeds = mux_speeds
    tool._test_pad_calls = pad_calls
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
    # The plate acts the line padded with silence to its clip's length (226 frames, 9.417 s) ...
    assert tool._test_pad_calls == [("line.wav", 9.417)]
    assert plate_payload["target_audio_url"] == "https://up.example/.take.plate.wav"
    assert plate_payload["duration"] == 9  # the fewest whole seconds whose clip holds the 9.0 s line
    # ... and the lip-sync pass follows the line itself.
    sync_payload = mock_post.call_args_list[1].kwargs["json"]
    assert sync_payload == {"video_url": "https://media.example/plate.mp4",
                            "audio_url": "https://up.example/line.wav",
                            "duration_mode": "cut_off", "resolution": "768P"}
    assert result.cost_usd == round(0.056 * 9 + 0.20 * 9, 4)
    assert result.data["cost_breakdown"] == {"plate": 0.504, "sync": 1.8}
    assert not list((tmp_path).glob(".*.plate.wav"))  # the padded copy is not left behind


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
    assert tool._test_mux_calls == [9.0]  # trimmed to the line, not the clip's silent tail
    assert tool._test_mux_speeds == [None]  # and played as it came
    # No model named: the take is made on H3 Max Turbo, and says so.
    assert mock_post.call_args.args == ("https://queue.fal.run/minimax/h3-max-turbo/image-to-video",)
    assert result.cost_usd == round(0.028 * 9, 4)  # 9 s of Turbo plate at the 768P rate, nothing else
    assert result.data["mode"] == "native" and result.data["model"] == "h3max-turbo"
    assert result.data["endpoints"] == ["minimax/h3-max-turbo/image-to-video"]
    assert result.model == "minimax/h3-max-turbo/image-to-video"
    assert result.data["request_ids"] == ["rid-plate"]


def test_the_models_clip_lengths_and_the_seconds_asked_for():
    """H3 Max returns clips of 22 + 17n frames at 24 a second. Read off every take of videos
    713 to 716 (asked 4, 5, 6, 7, 8 s: 107, 124, 158, 175, 192 frames); the first of these
    lengths, 22 frames, is the 0.92 s the model gives as its shortest clip."""
    for asked, frames in [(2, 56), (4, 107), (5, 124), (6, 158), (7, 175), (8, 192), (15, 362)]:
        assert FalAvatar._clip_frames(asked) == frames
    # A line is asked for as the fewest whole seconds whose clip holds it and a short tail.
    for line, asked in [(4.133, 4), (7.355, 8), (5.52, 6), (5.01, 5), (3.30, 4), (1.0, 2),
                        (10.0, 10), (14.5, 15)]:
        assert FalAvatar._plate_seconds(line) == asked
        clip = FalAvatar._clip_frames(asked) / 24
        assert clip >= line + FalAvatar._PLATE_TAIL_SECONDS - 1e-6
        if asked > 2:  # and one second fewer would not have held it
            assert FalAvatar._clip_frames(asked - 1) / 24 < line + FalAvatar._PLATE_TAIL_SECONDS
    # 4.44 s still fits the 107-frame clip (4.458 s) only without its tail: it moves up a step.
    assert FalAvatar._plate_seconds(4.35) == 4 and FalAvatar._plate_seconds(4.44) == 5


def test_the_model_is_sent_the_line_padded_to_the_length_of_its_clip(tmp_path, monkeypatch):
    """The model lays the performance of its audio over the whole clip it returns. Video 713's
    hook, a 4.13 s line, was sent as it was and came back in a 5.17 s clip: the lips ran at 80%
    of the voice's pace and were a second behind by the last word. Sent with silence up to the
    clip's length (4.458 s, asked for as 4 s), the lips kept the voice's pace."""
    tool, audio = _chain_tool(monkeypatch, tmp_path, audio_seconds=4.133, clip_seconds=107 / 24)
    out = tmp_path / "take_A.mp4"
    post = _response(json_data={"status_url": "https://queue.fal.run/x/requests/rp/status",
                                "response_url": "https://queue.fal.run/x/requests/rp",
                                "request_id": "rid-plate"})
    results = iter([
        _response(json_data={"status": "COMPLETED"}),
        _response(json_data={"video": {"url": "https://media.example/plate.mp4"}}),
        _response(content=b"fake-plate-mp4"),
    ])
    with (
        patch("requests.post", return_value=post) as mock_post,
        patch("requests.get", side_effect=lambda *a, **k: next(results)),
    ):
        result = tool.execute({"image_path": str(tmp_path / "face.jpg"), "audio_path": str(audio),
                               "output_path": str(out)})

    assert result.success is True, result.error
    payload = mock_post.call_args.kwargs["json"]
    assert payload["duration"] == 4
    assert tool._test_pad_calls == [("line.wav", 4.458)]
    assert payload["target_audio_url"] == "https://up.example/.take_A.plate.wav"
    job = result.data["resume_job"]
    assert job["plate_seconds"] == 4 and job["plate_audio_seconds"] == 4.458
    assert job["audio_url"] == "https://up.example/line.wav"  # the line itself stays the job's audio
    assert result.cost_usd == round(0.028 * 4, 4)  # fal bills the seconds asked for (billed $0.112)
    # The clip came back as long as the audio it was sent: cut at the line's end, nothing else.
    assert tool._test_mux_calls == [4.133] and tool._test_mux_speeds == [None]
    assert "plate_fit" not in job and "note" not in result.data


def test_a_clip_of_an_unexpected_length_is_played_at_the_pace_of_its_audio(tmp_path, monkeypatch):
    """The safety net. Should the model return a clip that is not as long as the audio it was
    sent, its lips run at audio / clip of the voice's pace; played faster by clip / audio they
    are back on the words (video 713's takes, fitted by hand, shut at the right sounds)."""
    tool, audio = _chain_tool(monkeypatch, tmp_path, audio_seconds=4.133, clip_seconds=124 / 24)
    out = tmp_path / "take_A.mp4"
    post = _response(json_data={"status_url": "https://queue.fal.run/x/requests/rp/status",
                                "response_url": "https://queue.fal.run/x/requests/rp"})
    results = iter([
        _response(json_data={"status": "COMPLETED"}),
        _response(json_data={"video": {"url": "https://media.example/plate.mp4"}}),
        _response(content=b"fake-plate-mp4"),
    ])
    with (
        patch("requests.post", return_value=post),
        patch("requests.get", side_effect=lambda *a, **k: next(results)),
    ):
        result = tool.execute({"image_path": str(tmp_path / "face.jpg"), "audio_path": str(audio),
                               "output_path": str(out)})

    assert result.success is True, result.error
    fit = round((124 / 24) / 4.458, 4)
    assert tool._test_mux_calls == [4.133] and tool._test_mux_speeds == [fit]
    assert result.data["resume_job"]["plate_fit"] == fit
    assert "returned a clip of 5.167 s" in result.data["note"]
    # Within a frame and a half there is nothing to fit; an unreadable clip is left as it is.
    assert FalAvatar._plate_fit(107 / 24, 4.458) is None
    assert FalAvatar._plate_fit(108 / 24, 4.458) is None
    assert FalAvatar._plate_fit(109 / 24, 4.458) == round((109 / 24) / 4.458, 4)
    assert FalAvatar._plate_fit(None, 4.458) is None and FalAvatar._plate_fit(5.167, None) is None


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")
def test_padding_fitting_and_cutting_with_the_real_ffmpeg(tmp_path):
    """The three local steps on real files: the padded audio is exactly the clip's length, a clip
    is counted in frames, and a fitted take holds the frames of the line and no more."""
    line, clip = tmp_path / "line.wav", tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5",
                    str(line)], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=96x144:rate=24",
                    "-frames:v", "56", "-pix_fmt", "yuv420p", str(clip)], check=True)
    padded = FalAvatar._pad_audio(line, tmp_path / "padded.wav", 56 / 24)
    assert abs(FalAvatar._probe_duration(str(padded)) - 56 / 24) < 0.001
    assert FalAvatar._clip_facts(clip) == (56, 24.0)
    assert FalAvatar._clip_seconds(clip) == pytest.approx(56 / 24)
    assert FalAvatar._clip_seconds(tmp_path / "missing.mp4") is None
    # As it came: cut at the line's end (1.5 s is 36 frames).
    plain = FalAvatar._mux_audio(clip, line, tmp_path / "plain.mp4", trim_seconds=1.5)
    assert FalAvatar._clip_facts(plain) == (36, 24.0)
    # Played 1.5 times faster the whole 56-frame clip lasts 1.56 s: still 24 frames a second,
    # still cut at 1.5 s, and the sound is the line's, as long as it was.
    fitted = FalAvatar._mux_audio(clip, line, tmp_path / "fitted.mp4", trim_seconds=1.5, speed=1.5)
    assert FalAvatar._clip_facts(fitted) == (36, 24.0)
    assert FalAvatar._probe_duration(str(fitted)) == pytest.approx(1.5, abs=0.06)



def test_a_take_is_recorded_at_the_plate_length_it_asked_for(tmp_path, monkeypatch):
    """fal's answer for a plate carries no duration, and fal bills the seconds the plate was
    asked for (video 714: asked 7, billed 7; the test take of 2026-11-10: asked 4, billed 4).
    So the recorded cost, like the quote, is the asked length's: 6 s for a 5.54 s line."""
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
    # On the full model, as video 714's take was made: the model is named.
    inputs = {"image_path": str(tmp_path / "face.jpg"), "audio_path": str(audio), "output_path": str(out),
              "model": "h3max"}
    assert tool.estimate_cost(inputs) == 0.336  # the quote the budget is held to says the same
    assert tool.estimate_cost({k: v for k, v in inputs.items() if k != "model"}) == 0.168  # Turbo, the default
    with (
        patch("requests.post", return_value=post) as mock_post,
        patch("requests.get", side_effect=lambda *a, **k: next(results)),
    ):
        result = tool.execute(inputs)

    assert result.success is True, result.error
    assert mock_post.call_args.kwargs["json"]["duration"] == 6  # its clip, 158 frames, holds 5.542 s
    assert result.cost_usd == 0.336
    assert result.data["cost_breakdown"] == {"plate": 0.336}
    assert result.data["resume_job"]["plate_seconds"] == 6
    # The job keeps the model that really made the plate: a resumed job is priced by it.
    assert mock_post.call_args.args == ("https://queue.fal.run/minimax/h3-max/image-to-video",)
    assert result.data["resume_job"]["plate_endpoint"] == "minimax/h3-max/image-to-video"
    assert result.data["model"] == "h3max" and result.data["endpoints"] == ["minimax/h3-max/image-to-video"]
    assert tool._test_mux_calls == [5.542]  # the delivered take is still cut to the line


def test_each_take_model_has_its_plate_and_its_rates():
    """H3 Max Turbo takes the same inputs and returns the same clip lengths as H3 Max, at half
    the price: video 717's hook take made on both was billed $0.112 against $0.224 for the 4
    seconds asked. Turbo is the default; `h3max` keeps the full model; the composite's plate
    stays on the full model, as does a job saved before the model keys existed."""
    assert FalAvatar.NATIVE == "h3max-turbo" and FalAvatar.NATIVE_FULL == "h3max"
    assert FalAvatar._plate("h3max-turbo") == ("minimax/h3-max-turbo/image-to-video", 0.028, 0.0175)
    assert FalAvatar._plate("h3max") == ("minimax/h3-max/image-to-video", 0.056, 0.035)
    assert FalAvatar._plate("h3max-acting") == ("minimax/h3-max/image-to-video", 0.056, 0.035)
    assert FalAvatar._plate(None) == ("minimax/h3-max/image-to-video", 0.056, 0.035)
    tool = FalAvatar()
    assert tool._plate_rate({}) == 0.028 and tool._plate_rate({"resolution": "480P"}) == 0.0175
    assert tool._plate_rate({}, "h3max") == 0.056 and tool._plate_rate({"model": "h3max"}) == 0.056
    schema = tool.input_schema["properties"]["model"]
    assert schema["default"] == "h3max-turbo" and schema["enum"][:3] == ["h3max-turbo", "h3max", "h3max-acting"]
    # The two real takes, as fal billed them: 4 s asked for a 3.18 s line.
    assert tool.estimate_cost({"audio_duration_seconds": 3.183}) == 0.112
    assert tool.estimate_cost({"audio_duration_seconds": 3.183, "model": "h3max"}) == 0.224


def test_a_missing_audio_file_is_named_not_called_a_pricing_problem(tmp_path, monkeypatch):
    """A take is priced by its audio's length. When the file was not where the call said, the
    quote answered "cannot quote a price", the job refused the call as unpriced, and the
    director of video 716 went reading the tool's source to learn that a path was wrong."""
    from tools.base_tool import _budget_quote

    tool = FalAvatar()
    missing = str(tmp_path / "assets" / "audio" / "voice_A.wav")
    with pytest.raises(FileNotFoundError, match="audio file not found: .*voice_A.wav"):
        tool.estimate_cost({"image_path": "face.png", "audio_path": missing})
    # For the budget check that is "no quote", not "no price": the call may go on, and it then
    # fails on the file by name before anything is uploaded or spent.
    assert _budget_quote(tool, {"image_path": "face.png", "audio_path": missing}) == (None, False)
    monkeypatch.setenv("FAL_KEY", "test-key")
    with patch("requests.post") as mock_post:
        result = tool.execute({"image_path": str(tmp_path / "face.png"), "audio_path": missing,
                               "output_path": str(tmp_path / "take.mp4")})
    assert result.success is False and "File not found" in result.error and "voice_A.wav" in result.error
    mock_post.assert_not_called()
    # With no audio named at all, the price really cannot be quoted, and that is still said so.
    with pytest.raises(PriceQuoteRequired):
        tool.estimate_cost({"image_url": "https://x/p.png", "audio_url": "https://x/a.mp3"})
    assert _budget_quote(tool, {"image_url": "https://x/p.png", "audio_url": "https://x/a.mp3"}) == (None, True)
