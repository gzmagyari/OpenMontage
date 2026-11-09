"""The soundtrack: every prepared voice part at its place on the spine, the music under it.

The spine in work/kit/voice.json is the one source of line offsets for both the picture and
the sound, so they cannot drift apart. A video without a voice gets its music alone. The bed
is cut to the video's length and faded out, so it never stops dead. Loudness lands on
-14 LUFS; `kit render` checks it again on the finished file.
"""

from __future__ import annotations

import json
import subprocess
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
    edit = json.loads((project / "composition" / "edit.json").read_text(encoding="utf-8"))
    scripted = bool((edit.get("voice") or {}).get("lines"))
    voice: dict[str, Any] = {"lines": []}
    if scripted:
        try:
            voice = json.loads((work / "voice.json").read_text(encoding="utf-8"))
        except OSError:
            raise MixError("no voice timings: run `kit voice` first") from None
    tracks: list[dict[str, Any]] = []
    for line in voice["lines"]:
        for part in line["parts"]:
            path = project / part["audio"]
            if not path.is_file():
                raise MixError(f"{part['audio']} is missing: run `kit voice` again")
            tracks.append({"path": str(path), "role": "speech", "start_seconds": float(part["start"])})
    spoken = len(tracks)
    music = edit.get("music") or {}
    if music.get("file"):
        source = project / str(music["file"])
        if not source.is_file():
            raise MixError(f"the music file {music['file']} does not exist yet")
        if total_seconds:
            fade = min(float(music.get("fade_out", 1.2)), total_seconds / 3)
            cut = work / "music_cut.wav"
            cut.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(source), "-t", f"{total_seconds:.3f}",
                            "-af", f"afade=t=out:st={max(0.0, total_seconds - fade):.3f}:d={fade:.3f}", "-ar", "48000", str(cut)],
                           check=True, timeout=180)
            source = cut
        # Under a voice the bed sits low; on its own it IS the soundtrack.
        tracks.append({"path": str(source), "role": "music", "volume": float(music.get("volume", 0.3 if spoken else 0.9))})
    if not tracks:
        raise MixError("nothing to mix: the edit has neither voice lines nor music")
    from tools.audio.audio_mixer import AudioMixer
    output = project / MIX_PATH
    output.parent.mkdir(parents=True, exist_ok=True)
    inputs: dict[str, Any] = {"operation": "full_mix", "tracks": tracks, "ducking": {"enabled": bool(spoken)},
                              "normalize": True, "loudnorm_target": TARGET_LUFS, "output_path": str(output)}
    if total_seconds:
        inputs["target_duration"] = float(total_seconds)
    result = AudioMixer().execute(inputs)
    if not result.success:
        raise MixError(f"the mix failed: {result.error}")
    return {"file": MIX_PATH, "voice_parts": spoken, "music": music.get("file"), "seconds": total_seconds}
