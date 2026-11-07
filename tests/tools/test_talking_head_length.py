"""A TalkingHead render is as long as its video, not the composition's 5-minute limit.

Rendered as it stood, a 10-second ad produced 9000 frames and timed out (job_699895e4006e4c75).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.video.video_compose import VideoCompose


@pytest.fixture
def render(monkeypatch):
    """Drive the render with the Remotion CLI stubbed; returns (tool, the props the CLI would get)."""
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name: real_which(name) or f"/usr/bin/{name}")
    tool = VideoCompose()
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        props = next(arg for arg in cmd if arg.startswith("--props="))
        seen.update(json.loads(Path(props.split("=", 1)[1]).read_text(encoding="utf-8")))

    monkeypatch.setattr(tool, "run_command", fake_run)
    return tool, seen


def _inputs(tmp_path: Path, **composition) -> dict:
    return {
        "output_path": str(tmp_path / "renders" / "final.mp4"),
        "public_dir": str(tmp_path / "public"),
        "composition_data": {"renderer_family": "presenter", "videoSrc": "take.mp4", "captions": [], **composition},
    }


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_the_render_takes_its_length_from_the_video(render, tmp_path):
    tool, props = render
    (tmp_path / "public").mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=2", str(tmp_path / "public" / "take.mp4")],
                   check=True)
    tool._remotion_render(_inputs(tmp_path))
    assert props["durationSeconds"] == pytest.approx(2.0, abs=0.1)


def test_an_explicit_length_wins(render, tmp_path, monkeypatch):
    tool, props = render
    (tmp_path / "public").mkdir()
    monkeypatch.setattr(tool, "_local_video_seconds", lambda *a: pytest.fail("no probe needed"))
    tool._remotion_render(_inputs(tmp_path, durationSeconds=7))
    assert props["durationSeconds"] == 7


def test_a_missing_video_is_left_to_the_composition(render, tmp_path):
    tool, props = render
    (tmp_path / "public").mkdir()
    tool._remotion_render(_inputs(tmp_path))
    assert "durationSeconds" not in props


def test_a_remote_video_is_not_probed():
    assert VideoCompose._local_video_seconds("https://x.example/take.mp4", None, Path(".")) is None
