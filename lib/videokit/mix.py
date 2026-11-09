"""The soundtrack: every prepared voice part at its place on the spine, the music under it.

The spine in work/kit/voice.json is the one source of line offsets for both the picture and
the sound, so they cannot drift apart. Loudness lands on -14 LUFS; `kit render` checks it
again on the finished file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TARGET_LUFS = -14.0
MIX_PATH = "assets/audio/mix.wav"


class MixError(Exception):
    pass


def build(project: Path, *, total_seconds: float | None = None) -> dict[str, Any]:
    """Mix voice and music into assets/audio/mix.wav. `total_seconds` is the video's length
    (the soundtrack is padded or trimmed to it); without it the mix ends with the voice."""
    work = project / "work" / "kit"
    try:
        voice = json.loads((work / "voice.json").read_text(encoding="utf-8"))
    except OSError:
        raise MixError("no voice timings: run `kit voice` first") from None
    edit = json.loads((project / "composition" / "edit.json").read_text(encoding="utf-8"))
    tracks: list[dict[str, Any]] = []
    for line in voice["lines"]:
        for part in line["parts"]:
            path = project / part["audio"]
            if not path.is_file():
                raise MixError(f"{part['audio']} is missing: run `kit voice` again")
            tracks.append({"path": str(path), "role": "speech", "start_seconds": float(part["start"])})
    music = edit.get("music") or {}
    if music.get("file"):
        path = project / str(music["file"])
        if not path.is_file():
            raise MixError(f"the music file {music['file']} does not exist yet")
        tracks.append({"path": str(path), "role": "music", "volume": float(music.get("volume", 0.3)),
                       "fade_out_seconds": float(music.get("fade_out", 1.2))})
    from tools.audio.audio_mixer import AudioMixer
    output = project / MIX_PATH
    output.parent.mkdir(parents=True, exist_ok=True)
    inputs: dict[str, Any] = {"operation": "full_mix", "tracks": tracks, "ducking": {"enabled": True},
                              "normalize": True, "loudnorm_target": TARGET_LUFS, "output_path": str(output)}
    if total_seconds:
        inputs["target_duration"] = float(total_seconds)
    result = AudioMixer().execute(inputs)
    if not result.success:
        raise MixError(f"the mix failed: {result.error}")
    return {"file": MIX_PATH, "voice_parts": sum(1 for t in tracks if t["role"] == "speech"),
            "music": music.get("file"), "seconds": total_seconds}
