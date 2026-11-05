"""full_mix must honor target_duration and stay stereo when speech ends early (seen with FFmpeg 8.1).

Regression: with mono narration that ended before the music, the mix came out truncated at
the last speech sample and in mono, so late SFX / the music tail were lost.
"""

import json
import re
import shutil
import subprocess

import pytest

from tools.audio.audio_mixer import AudioMixer

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg required")


def _tone(path, seconds, freq, channels):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
         "-ac", str(channels), str(path)],
        check=True,
    )


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=channels:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    return float(data["format"]["duration"]), int(data["streams"][0]["channels"])


def _mean_db(path, start, length):
    err = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(length), "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stderr
    match = re.search(r"mean_volume: (-?[\d.]+) dB", err)
    return float(match.group(1)) if match else -999.0


@pytest.mark.parametrize("normalize", [True, False])
def test_full_mix_pads_to_target_and_stays_stereo(tmp_path, normalize):
    speech_a, speech_b = tmp_path / "speech_a.wav", tmp_path / "speech_b.wav"
    music, sfx = tmp_path / "music.wav", tmp_path / "sfx.wav"
    _tone(speech_a, 1.5, 440, 1)
    _tone(speech_b, 1.0, 550, 1)
    _tone(music, 6.0, 220, 2)  # shorter than the target as well
    _tone(sfx, 0.4, 880, 2)
    out = tmp_path / "mix.wav"
    result = AudioMixer().execute({
        "operation": "full_mix",
        "tracks": [
            {"path": str(speech_a), "role": "speech", "start_seconds": 0.5},
            {"path": str(speech_b), "role": "speech", "start_seconds": 3.0},
            {"path": str(music), "role": "music", "volume": 0.5},
            {"path": str(sfx), "role": "sfx", "start_seconds": 7.5},
        ],
        "ducking": {"enabled": True, "music_volume_during_speech": 0.25, "attack_ms": 25, "release_ms": 220},
        "normalize": normalize,
        "loudnorm_target": -14,
        "target_duration": 9.0,
        "output_path": str(out),
    })
    assert result.success, result.error
    seconds, channels = _probe(out)
    assert abs(seconds - 9.0) < 0.05, seconds
    assert channels == 2
    # the SFX after the last speech must survive (it was cut off with the truncation bug)
    assert _mean_db(out, 7.55, 0.3) > -40
