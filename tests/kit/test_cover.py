"""`kit cover`: one frame of the finished video as the picture it is posted with."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from lib.videokit import cli, cover

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def _video(path: Path, seconds: float = 3.0) -> Path:
    """A small vertical video whose every frame differs, tagged as the kit tags its exports."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=270x480:rate=30:duration={seconds}",
         "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
         "-color_range", "tv", str(path)], check=True, timeout=120)
    return path


@pytest.fixture()
def project(tmp_path) -> Path:
    (tmp_path / "composition").mkdir()
    _video(tmp_path / "exports" / "video.mp4")
    return tmp_path


def _size(path: Path) -> tuple[int, int]:
    done = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    width, height = done.stdout.strip().split(",")[:2]
    return int(width), int(height)


def test_the_cover_is_a_full_size_frame_of_the_video(project):
    made = cover.make(project, at=1.5)
    target = project / "exports" / "cover.jpg"
    assert made == {"file": "exports/cover.jpg", "at": 1.5, "video": "video.mp4", "width": 270, "height": 480,
                    "bytes": target.stat().st_size}
    assert target.read_bytes()[:3] == b"\xff\xd8\xff" and _size(target) == (270, 480)
    assert json.loads((project / "work" / "kit" / "cover.json").read_text(encoding="utf-8")) == made
    assert cover.is_current(project, project / "exports" / "video.mp4")
    assert not list((project / "exports").glob(".cover*")), "no half-written file is left behind"


def test_another_second_is_another_picture(project):
    first = (cover.make(project, at=0.5), (project / "exports" / "cover.jpg").read_bytes())[1]
    second = (cover.make(project, at=2.0), (project / "exports" / "cover.jpg").read_bytes())[1]
    assert first != second


def test_without_a_second_the_last_one_is_used_and_a_second_past_the_end_is_brought_back(project):
    assert cover.make(project)["at"] == 1.0, "one second in when nothing was ever chosen"
    assert cover.make(project, at=2.2)["at"] == 2.2
    assert cover.make(project)["at"] == 2.2, "a later version of the video keeps the frame the director chose"
    assert cover.make(project, at=99)["at"] == 2.5, "half a second before the end, never the fade's last frame"
    assert cover.make(project, at=-3)["at"] == 0.0


def test_a_cover_made_before_the_newest_render_is_out_of_date(project):
    cover.make(project, at=1.0)
    video = project / "exports" / "video.mp4"
    assert cover.is_current(project, video)
    later = time.time() + 30
    os.utime(video, (later, later))
    assert not cover.is_current(project, video)
    # The newest export is the one a cover is taken from.
    newer = _video(project / "exports" / "video_rev2.mp4", seconds=2.0)
    os.utime(newer, (later + 30, later + 30))
    assert cover.make(project)["video"] == "video_rev2.mp4"
    assert cover.make(project, video=video)["video"] == "video.mp4"


def test_no_video_no_cover(tmp_path):
    (tmp_path / "composition").mkdir()
    with pytest.raises(cover.CoverError, match="no finished video"):
        cover.make(tmp_path)
    (tmp_path / "exports").mkdir()
    (tmp_path / "exports" / "video.mp4").write_bytes(b"not a video at all")
    with pytest.raises(cover.CoverError, match="could not be read as a video"):
        cover.make(tmp_path)
    assert not (tmp_path / "exports" / "cover.jpg").exists()
    assert not cover.is_current(tmp_path, tmp_path / "exports" / "video.mp4")


def test_the_command(project, capsys):
    assert cli.main(["cover", "--at", "1.25", "--project", str(project)]) == 0
    said = capsys.readouterr().out
    assert "the frame at 1.25 s of video.mp4 is exports/cover.jpg (270x480)" in said
    assert cli.main(["cover", "--at", "soon", "--project", str(project)]) == 2
    assert "--at takes a second of the video" in capsys.readouterr().err
    empty = project / "empty"
    (empty / "composition").mkdir(parents=True)
    assert cli.main(["cover", "--project", str(empty)]) == 2
    assert "run `kit render` first" in capsys.readouterr().err
    assert "kit cover [--at SECONDS]" in (cli.__doc__ or "")
