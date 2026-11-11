"""The cover picture: one frame of the finished video, saved at full size.

A short video is posted with a cover (the picture a profile grid and a share show). It is a frame
of the video itself, so it costs nothing: `kit cover --at <second>` saves that frame of the newest
export as exports/cover.jpg. The second that was used is kept in work/kit/cover.json, so a later
version of the video (a revision) gets its cover from the same place.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

COVER_PATH = "exports/cover.jpg"
RECORD_PATH = "work/kit/cover.json"
DEFAULT_AT = 1.0
# How close to the end a cover may be taken: the last frames of a video are its fade.
END_MARGIN = 0.5
# The video is BT.709 with limited range; a JPEG is read as BT.601 with full range. Without the
# conversion the cover's colours sit a shade off the video's.
_COLOUR = "scale=in_color_matrix=bt709:out_color_matrix=bt601:in_range=tv:out_range=pc,format=yuvj420p"


class CoverError(Exception):
    pass


def _probe(video: Path) -> tuple[float, int, int]:
    """(seconds, width, height) of the video's picture."""
    try:
        done = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height:format=duration", "-of", "json", str(video)],
            capture_output=True, text=True, timeout=60)
        data = json.loads(done.stdout or "{}")
        stream = (data.get("streams") or [{}])[0]
        return float((data.get("format") or {}).get("duration") or 0), int(stream.get("width") or 0), int(stream.get("height") or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0, 0, 0


def newest_export(project: Path) -> Path | None:
    exports = project / "exports"
    videos = sorted((p for p in exports.glob("*.mp4") if p.is_file()), key=lambda p: p.stat().st_mtime) if exports.is_dir() else []
    return videos[-1] if videos else None


def remembered_at(project: Path) -> float | None:
    """The second the project's last cover was taken at, if one was made."""
    try:
        value = json.loads((project / RECORD_PATH).read_text(encoding="utf-8")).get("at")
        return float(value) if isinstance(value, (int, float)) and value >= 0 else None
    except (OSError, ValueError, AttributeError):
        return None


def make(project: Path, *, at: float | None = None, video: Path | None = None) -> dict[str, Any]:
    """Save one frame of `video` (the newest export when not named) as exports/cover.jpg.
    `at` is the second of the frame; without it, the second the last cover used, else one
    second in. Raises CoverError when there is no video or the frame cannot be read."""
    source = video or newest_export(project)
    if source is None or not source.is_file():
        raise CoverError("there is no finished video in exports/ yet: run `kit render` first")
    seconds, width, height = _probe(source)
    if seconds <= 0 or width <= 0:
        raise CoverError(f"{source.name} could not be read as a video")
    wanted = at
    if wanted is None:
        wanted = remembered_at(project)
    if wanted is None:
        wanted = DEFAULT_AT
    moment = round(min(max(0.0, float(wanted)), max(0.0, seconds - END_MARGIN)), 3)
    target = project / COVER_PATH
    part = target.with_name(".cover.part.jpg")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{moment:.3f}", "-i", str(source), "-frames:v", "1",
             "-vf", _COLOUR, "-q:v", "2", str(part)],
            capture_output=True, text=True, timeout=120)
        if done.returncode != 0 or not part.is_file() or part.stat().st_size < 1000:
            raise CoverError(f"no frame could be read at {moment:.2f} s of {source.name}"
                             + (f" ({done.stderr.strip().splitlines()[-1][:160]})" if done.stderr.strip() else ""))
        part.replace(target)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CoverError(f"the cover could not be written: {exc}") from None
    finally:
        part.unlink(missing_ok=True)
    made = {"file": COVER_PATH, "at": moment, "video": source.name, "width": width, "height": height,
            "bytes": target.stat().st_size}
    record = project / RECORD_PATH
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(made, indent=1), encoding="utf-8")
    return made


def is_current(project: Path, video: Path) -> bool:
    """True when exports/cover.jpg exists and was made from (or after) this video."""
    cover = project / COVER_PATH
    try:
        return cover.is_file() and cover.stat().st_size > 0 and cover.stat().st_mtime >= video.stat().st_mtime
    except OSError:
        return False
