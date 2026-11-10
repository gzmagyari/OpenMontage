"""Generate music with Google Lyria 3.5 through fal.ai.

The default music bed for videos since 2026-11-10: $0.10 a track, flat, against $0.60 per
started minute for ElevenLabs Music. Uses OpenMontage's shared ``FAL_KEY`` credential and the
fal.ai queue API, then downloads the generated MP3 to a project-local path.

What the model does, measured on three tracks (2026-11-10):
- its only input is the prompt: no length field, no negative prompt;
- it returns about a minute (59.8 to 63.4 s) even when the prompt asks for 30 seconds, and
  stretches a timed plan over that minute, so a length here is a wish, not a contract. The
  video kit's mix cuts the music at the video's end and fades it out, so a longer track is
  simply cut; this tool asks for more than the video needs and reports what really came back;
- a section plan with timestamps is followed in order (six asked, six returned), not to the
  second (an intro asked for 4 s ran 10 s);
- it takes 26 to 33 s of model time;
- asked for an instrumental it returns one, and says so: its ``lyrics`` field then holds
  section markers alone (``[[A0]]``, ``[[B1]]`` ...).
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class FalLyriaMusic(BaseTool):
    """Generate a music track with Lyria 3.5 through fal.ai."""

    name = "fal_lyria_music"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "music_generation"
    provider = "fal.ai"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:FAL_KEY"]
    install_instructions = (
        "Set FAL_KEY to a fal.ai API key. "
        "Get one at https://fal.ai/dashboard/keys"
    )
    fallback_tools = ["fal_elevenlabs_music", "music_gen"]
    agent_skills = ["lyria", "music"]

    capabilities = [
        "generate_background_music",
        "generate_instrumental",
    ]
    supports = {
        "instrumental": True,
        "style_control": True,
        "exact_duration": False,
    }
    best_for = [
        "the music bed of a short video: made to order for a flat $0.10",
        "music in a style, mood and instrumentation the client asked for",
        "a section plan followed in order (intro, groove, lift, ending)",
    ]
    not_good_for = [
        "an exact length or a hit on an exact second (use fal_elevenlabs_music)",
        "tracks under a minute as delivered: it returns about a minute and the mix cuts it",
        "offline generation",
    ]

    input_schema = {
        "type": "object",
        "required": ["prompt", "duration_seconds"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The music: genre, mood, instruments, tempo, and optionally a section "
                               "plan with timestamps (followed in order, not to the second)",
            },
            "duration_seconds": {
                "type": "number",
                "minimum": 3,
                "maximum": 170,
                "description": "The length the track has to cover: the video's length. The track that "
                               "comes back is longer (about a minute, more when this asks for more); "
                               "`kit mix` cuts it at the video's end and fades it out",
            },
            "force_instrumental": {
                "type": "boolean",
                "default": True,
                "description": "Ask for music without any voice",
            },
            "output_path": {
                "type": "string",
                "default": "fal_lyria_music.mp3",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1,
        ram_mb=256,
        vram_mb=0,
        disk_mb=50,
        network_required=True,
    )
    retry_policy = RetryPolicy(
        max_retries=0,
        retryable_errors=["rate_limit", "timeout"],
    )
    idempotency_key_fields = ["prompt", "duration_seconds", "force_instrumental"]
    side_effects = [
        "writes an MP3 file to output_path",
        "submits one paid fal.ai generation request",
    ]
    user_visible_verification = [
        "Listen to the generated music for mood and mix, and for a voice where none was asked",
    ]

    _MODEL = "google/lyria-3.5"
    _QUEUE_URL = f"https://queue.fal.run/{_MODEL}"
    # fal bills one generation, whatever its length (billing events for three tracks, 2026-11-10).
    _PRICE_USD = 0.10
    _POLL_INTERVAL_SECONDS = 2
    _MAX_WAIT_SECONDS = 600
    _INSTRUMENTAL = ("Instrumental only: no lead or backing vocals, no singing, no lyrics, no speech, "
                     "no choir, no humming, no vocal samples.")
    # Section markers ([[A0]]) and timings ([12.0:], [:]) are all an instrumental's `lyrics` holds.
    _LYRIC_MARKS = re.compile(r"\[\[[^\]]*\]\]|\[[\d.:\s]*\]")

    def _get_api_key(self) -> str | None:
        return os.environ.get("FAL_KEY") or os.environ.get("FAL_AI_API_KEY")

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if self._get_api_key() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        """$0.10 a generation, flat."""
        return self._PRICE_USD

    @staticmethod
    def _length_wish(needed_seconds: float) -> str:
        """What the prompt asks for so that the track covers ``needed_seconds``: the model's own
        minute for anything up to 50 s, otherwise the next half minute that leaves 10 s to spare."""
        if needed_seconds <= 50:
            return "A one-minute track."
        seconds = math.ceil((float(needed_seconds) + 10) / 30) * 30
        if seconds % 60 == 0:
            minutes = seconds // 60
            return f"A {minutes}-minute track."
        return f"A {seconds}-second track."

    @classmethod
    def _model_prompt(cls, prompt: str, needed_seconds: float, instrumental: bool) -> str:
        parts = [str(prompt).strip()]
        if instrumental:
            parts.append(cls._INSTRUMENTAL)
        parts.append(cls._length_wish(needed_seconds))
        return " ".join(part for part in parts if part)

    @classmethod
    def _sung_words(cls, lyrics: Any) -> str:
        """The words of the returned ``lyrics`` once its section markers and timings are taken
        out: empty for an instrumental."""
        if not isinstance(lyrics, str):
            return ""
        return " ".join(cls._LYRIC_MARKS.sub(" ", lyrics).split())

    @staticmethod
    def _probe_seconds(path: Path) -> float | None:
        if not shutil.which("ffprobe"):
            return None
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=30, check=False)
            return float(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="No fal.ai API key found. " + self.install_instructions,
            )

        needed = inputs.get("duration_seconds")
        if needed is None:
            return ToolResult(success=False, error="duration_seconds is required")
        needed = float(needed)
        if not 3 <= needed <= 170:
            return ToolResult(
                success=False,
                error="duration_seconds must be between 3 and 170",
            )
        if not str(inputs.get("prompt") or "").strip():
            return ToolResult(success=False, error="prompt is required")

        import requests

        started = time.time()
        headers = {
            "Authorization": f"Key {api_key}",
            "Content-Type": "application/json",
        }
        instrumental = bool(inputs.get("force_instrumental", True))
        payload = {"prompt": self._model_prompt(inputs["prompt"], needed, instrumental)}

        try:
            submit_response = requests.post(
                self._QUEUE_URL,
                headers=headers,
                json=payload,
                timeout=30,
            )
            submit_response.raise_for_status()
            queue_data = submit_response.json()
            status_url = queue_data["status_url"]
            response_url = queue_data["response_url"]

            deadline = time.monotonic() + self._MAX_WAIT_SECONDS
            while True:
                if time.monotonic() >= deadline:
                    return ToolResult(
                        success=False,
                        error="fal.ai music generation timed out while waiting in the queue",
                        duration_seconds=round(time.time() - started, 2),
                    )
                time.sleep(self._POLL_INTERVAL_SECONDS)
                status_response = requests.get(status_url, headers=headers, timeout=20)
                status_response.raise_for_status()
                status = status_response.json().get("status", "UNKNOWN")
                if status == "COMPLETED":
                    break
                if status in {"FAILED", "CANCELLED"}:
                    return ToolResult(
                        success=False,
                        error=f"fal.ai music generation {status.lower()}",
                        duration_seconds=round(time.time() - started, 2),
                    )

            result_response = requests.get(response_url, headers=headers, timeout=30)
            if result_response.status_code >= 400:
                # fal's own words (a refused prompt, for one) instead of a bare status code.
                raise RuntimeError(f"fal.ai result fetch failed ({result_response.status_code}): "
                                   f"{str(result_response.text)[:600]}")
            result_data = result_response.json()
            audio_url = result_data["audio"]["url"]

            audio_response = requests.get(audio_url, timeout=120)
            audio_response.raise_for_status()
            if not audio_response.content:
                raise ValueError("Empty fal audio output")
            output_path = Path(inputs.get("output_path", "fal_lyria_music.mp3"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(audio_response.content)
        except Exception as exc:
            safe_error = str(exc).replace(api_key, "[REDACTED]")
            return ToolResult(
                success=False,
                error=f"fal.ai Lyria music generation failed: {safe_error}",
                duration_seconds=round(time.time() - started, 2),
            )

        actual = self._probe_seconds(output_path)
        words = self._sung_words(result_data.get("lyrics"))
        notes = []
        if actual is not None and actual + 0.05 < needed:
            notes.append(f"the track is {actual:.1f} s, shorter than the {needed:g} s asked for: the music "
                         "ends before the video does")
        if instrumental and words:
            notes.append("the model returned sung words although an instrumental was asked for; "
                         "listen before using it, or generate again")
        data = {
            "provider": "fal.ai",
            "model": self._MODEL,
            "prompt": inputs["prompt"],
            "model_prompt": payload["prompt"],
            # The file's real length; `needed_seconds` is what the caller asked it to cover.
            "duration_seconds": round(actual, 2) if actual is not None else None,
            "needed_seconds": needed,
            "force_instrumental": instrumental,
            "sung_words": bool(words),
            "output": str(output_path),
            "format": "mp3",
            # For the job's ledger: settlement checks the estimate against fal's billing by id.
            "request_ids": [str(queue_data["request_id"])] if queue_data.get("request_id") else [],
        }
        if notes:
            data["note"] = "; ".join(notes)
        return ToolResult(
            success=True,
            data=data,
            artifacts=[str(output_path)],
            cost_usd=self._PRICE_USD,
            duration_seconds=round(time.time() - started, 2),
            model=self._MODEL,
        )
