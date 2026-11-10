"""Audio-driven avatar video through fal.ai using the shared FAL credential.

Animates a person so they speak a provided audio track (audio-first workflow:
generate narration with a TTS tool, then produce the talking video from it).
Wraps the avatar models hosted on fal.ai, so no Kling, HeyGen or local GPU
setup is needed - only FAL_KEY.

Three modes:
- single-pass avatar models (kling-v2-*, veed-fabric, omnihuman): photo + audio -> talking video.
- ``h3max-acting``: free-form prompted ACTING with accurate lips, in two passes -
  an H3 Max action plate conditioned on the audio, then an H3 Max lip-sync pass,
  then the original audio re-muxed verbatim. The audio is level-checked before
  any paid call (a silent track would otherwise produce a dead-mouthed video).
- lip-sync fix: pass ``video_path``/``video_url`` instead of an image to re-sync
  the lips of ANY existing speech video to the audio (one pass + re-mux).
"""

from __future__ import annotations

import math
import mimetypes
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.provider_pricing import PriceQuoteRequired

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
    ResumeSupport,
)

# Verified pause-tightening filter (a hand-rolled variant with afade=t=out:st=0 once silenced
# a whole production run; this exact chain is the known-good one).
TRIM_FILTER = ("silenceremove=start_periods=1:start_silence=0.05:start_threshold=-40dB:"
               "stop_periods=-1:stop_silence=0.3:stop_threshold=-40dB,apad=pad_dur=0.15")


class FalAvatar(BaseTool):
    """Talking-avatar video from one photo (or video) + one audio file via fal.ai-hosted models."""

    name = "fal_avatar"
    version = "0.2.0"
    tier = ToolTier.GENERATE
    capability = "avatar"
    provider = "fal.ai"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = ["env:FAL_KEY"]
    install_instructions = (
        "Set FAL_KEY to a fal.ai API key. No Kling/HeyGen account or local GPU is needed. "
        "Get a fal.ai key at https://fal.ai/dashboard/keys"
    )
    fallback_tools = ["kling_avatar", "heygen_avatar", "talking_head"]
    agent_skills = ["fal-avatar"]

    capabilities = [
        "photo_to_talking_video",
        "audio_driven_lip_sync",
        "acted_talking_take",
        "lip_sync_fix",
        "model_selection",
    ]
    supports = {
        "image_to_video": True,
        "video_to_video": True,
        "audio_input": True,
        "multilingual": True,  # follows whatever language the audio speaks
        "offline": False,
    }
    best_for = [
        "acted, lip-accurate presenter takes from one portrait + TTS narration (model 'h3max-acting')",
        "keeping one exact voice across every take (the delivered soundtrack is the input audio, verbatim)",
        "fixing the lip-sync of an existing speech video to a new or corrected audio track",
        "avatar videos with only a fal.ai credential",
    ]
    not_good_for = [
        "clips without speech (use a video generation tool)",
        "scripted dialogue without an audio file (generate the audio first with a TTS tool)",
    ]

    resume_support = ResumeSupport.FROM_CHECKPOINT

    # single-pass model key -> (fal endpoint, USD per output second; fal live pricing API, 2026-11-03)
    _MODELS: dict[str, tuple[str, float]] = {
        "kling-v2-standard": ("fal-ai/kling-video/ai-avatar/v2/standard", 0.0562),
        "kling-v2-pro": ("fal-ai/kling-video/ai-avatar/v2/pro", 0.115),
        "veed-fabric-1.0": ("veed/fabric-1.0", 0.08),
        "omnihuman-1.5": ("fal-ai/bytedance/omnihuman/v1.5", 0.16),
    }
    COMPOSITE = "h3max-acting"
    # The plate alone, delivered: audio-conditioned acting with H3's native lips, trimmed to the
    # line and re-muxed with the verbatim voice. 78% cheaper than the composite; the owner judged
    # the native lips acceptable for standard takes (2026-11-09). Lip-critical shots upgrade via
    # the composite or the lip-fix mode on an existing take.
    NATIVE = "h3max"
    # Real billed rates from fal billing events, 2026-11-09 (the pricing API misleads here:
    # lip-sync lists "$0.05 per unit" where a unit is 0.25s -> $0.20/s, and image-to-video
    # lists the $0.035 480P base while 768P bills 1.6x -> $0.056/s). Fal rounds output
    # seconds UP. Settlement reconciles against billing events either way (lib/fal_billing).
    _PLATE_ENDPOINT = "minimax/h3-max/image-to-video"          # $0.056/s at 768P, $0.035 at 480P
    _SYNC_ENDPOINT = "minimax/h3-max/lip-sync/video-to-video"  # $0.05 per 0.25s unit = $0.20/s
    _PLATE_PER_S_480 = 0.035
    _PLATE_PER_S = 0.056
    _SYNC_PER_S = 0.20
    _POLL_INTERVAL_SECONDS = 5
    _MAX_WAIT_SECONDS = 1200
    _MIN_AUDIO_PEAK = 0.005  # below this the track is treated as silent and refused
    # The plate is asked for the line plus this much, so the last word is never cut short; the
    # lip-sync pass, or the native trim, cuts it back to the line.
    _PLATE_HEADROOM_SECONDS = 0.6

    @classmethod
    def _plate_seconds(cls, audio_seconds: float) -> int:
        """The plate length asked of fal for a line: whole seconds, the line plus headroom
        rounded up, between 2 and 15. It is also what fal bills. Counting the line alone priced
        video 714's 5.54 s take at 6 seconds; fal had been asked for 7 and billed 7."""
        return min(15, max(2, math.ceil(float(audio_seconds) + cls._PLATE_HEADROOM_SECONDS)))

    input_schema = {
        "type": "object",
        "anyOf": [
            {"required": ["image_url", "audio_url"]},
            {"required": ["image_url", "audio_path"]},
            {"required": ["image_path", "audio_url"]},
            {"required": ["image_path", "audio_path"]},
            {"required": ["video_url", "audio_url"]},
            {"required": ["video_url", "audio_path"]},
            {"required": ["video_path", "audio_url"]},
            {"required": ["video_path", "audio_path"]},
            {"required": ["resume_job"]},
        ],
        "properties": {
            "image_url": {"type": "string", "description": "Public URL of the portrait to animate"},
            "image_path": {"type": "string", "description": "Local portrait; uploaded to fal storage"},
            "audio_url": {"type": "string", "description": "Public URL of the speech audio (the voice delivered verbatim)"},
            "audio_path": {"type": "string", "description": "Local speech audio; uploaded to fal storage"},
            "video_url": {"type": "string", "description": "Existing speech video whose lips should be re-synced "
                                                           "to the audio (lip-sync fix mode; image is ignored)"},
            "video_path": {"type": "string", "description": "Local video for lip-sync fix mode; uploaded to fal storage"},
            "model": {
                "type": "string",
                "default": "h3max",
                "enum": ["h3max", "h3max-acting", "kling-v2-standard", "kling-v2-pro", "veed-fabric-1.0",
                         "omnihuman-1.5"],
                "description": "h3max = THE DEFAULT for standard takes: one audio-conditioned H3 Max acting "
                               "generation with native lips, trimmed to the line and delivered with the "
                               "verbatim voice ($0.056/s at 768P). h3max-acting = the same plate plus a "
                               "frame-accurate lip-sync pass (~$0.26/s at 768P) - for tight close-ups and "
                               "lip-critical shots, or re-sync an existing take via video_url/video_path "
                               "(+$0.20/s, no new plate). Single-pass avatars: kling-v2-standard $0.0562/s, "
                               "veed-fabric-1.0 $0.08/s, kling-v2-pro $0.115/s, omnihuman-1.5 $0.16/s",
            },
            "prompt": {
                "type": "string",
                "description": "Performance direction (h3max-acting, kling and omnihuman): the person, one natural "
                               "action, 'talks straight into the camera', setting/lighting. Stable traits only.",
            },
            "resolution": {
                "type": "string",
                "description": "h3max-acting: 480P/768P (default 768P; the lip-sync pass caps at 768P). "
                               "veed-fabric-1.0: 720p/480p. omnihuman-1.5: 720p/1080p.",
            },
            "trim_silence": {
                "type": "boolean",
                "default": False,
                "description": "h3max-acting / fix mode: tighten leading, trailing and long internal pauses of the "
                               "audio with a verified ffmpeg chain before use (recommended for ElevenLabs v3 output).",
            },
            "turbo_mode": {
                "type": "boolean",
                "description": "omnihuman-1.5 only: faster generation with a slight quality trade-off",
            },
            "audio_duration_seconds": {
                "type": "number",
                "description": "Audio length, for cost estimation when the audio is not a local file",
            },
            "output_path": {"type": "string"},
            "resume_job": {"type": "object"},
            "job_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1,
        ram_mb=256,
        vram_mb=0,
        disk_mb=400,
        network_required=True,
    )
    retry_policy = RetryPolicy(
        max_retries=0,
        retryable_errors=["rate_limit", "timeout"],
    )
    idempotency_key_fields = [
        "image_url",
        "image_path",
        "audio_url",
        "audio_path",
        "video_url",
        "video_path",
        "model",
        "prompt",
        "resolution",
        "trim_silence",
        "turbo_mode",
    ]
    side_effects = [
        "writes a video file to output_path",
        "may upload the local image/video/audio to fal.ai storage",
        "submits one or two paid fal.ai generation requests",
    ]
    user_visible_verification = [
        "Watch the clip: lips must follow the audio from the first word, and the face must stay the person's",
    ]

    def _get_api_key(self) -> str | None:
        return os.environ.get("FAL_KEY") or os.environ.get("FAL_AI_API_KEY")

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if self._get_api_key() else ToolStatus.UNAVAILABLE

    # -- audio handling (local analysis keeps silent tracks from reaching a paid call) -----
    @staticmethod
    def _probe_duration(path: str) -> float | None:
        if not shutil.which("ffprobe"):
            return None
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=15, check=False,
            )
            return float(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip() else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    @staticmethod
    def _audio_peak(path: Path) -> float | None:
        """Peak |sample| of the decoded audio, or None when ffmpeg is unavailable."""
        if not shutil.which("ffmpeg"):
            return None
        try:
            raw = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                 "-f", "s16le", "-"], capture_output=True, timeout=60, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        peak = 0
        for i in range(0, len(raw) - 1, 2):
            v = int.from_bytes(raw[i:i + 2], "little", signed=True)
            peak = max(peak, v if v >= 0 else -v)
        return peak / 32768.0

    @classmethod
    def _trim_silence(cls, src: Path, dst: Path) -> Path:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-af", TRIM_FILTER,
                        "-ar", "48000", "-ac", "1", str(dst)], check=True, timeout=120)
        return dst

    @staticmethod
    def _mux_audio(video: Path, audio: Path, dst: Path, trim_seconds: float | None = None) -> Path:
        """Replace the video's soundtrack with ``audio`` verbatim. Without a trim the video is
        stream-copied; with ``trim_seconds`` (native mode: cut the plate's headroom at the line's
        end) the video is re-encoded for a frame-exact cut - stream copy + -shortest could only
        cut at a keyframe and leave the presenter mouthing past the voice."""
        if trim_seconds is None:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(audio),
                            "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                            "-shortest", str(dst)], check=True, timeout=300)
        else:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(audio),
                            "-t", f"{float(trim_seconds):.3f}", "-map", "0:v", "-map", "1:a",
                            "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", "-b:a", "192k", "-shortest", str(dst)], check=True, timeout=600)
        return dst

    def _audio_seconds(self, inputs: dict[str, Any]) -> float | None:
        if inputs.get("audio_duration_seconds"):
            return float(inputs["audio_duration_seconds"])
        audio_path = inputs.get("audio_path")
        if audio_path and Path(audio_path).exists():
            return self._probe_duration(audio_path)
        return None

    def _plate_rate(self, inputs: dict[str, Any]) -> float:
        return self._PLATE_PER_S_480 if str(inputs.get("resolution") or "768P").upper() == "480P" \
            else self._PLATE_PER_S

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        seconds = self._audio_seconds(inputs)
        if seconds is None:
            raise PriceQuoteRequired(
                "Billing follows the audio length; pass audio_duration_seconds (or a local audio_path) to estimate"
            )
        # Fal bills whole seconds, rounded up (billing events, 2026-11-09): a pass that follows
        # the audio by the line's length, the plate by the length it is asked for.
        line = math.ceil(float(seconds))
        if inputs.get("video_url") or inputs.get("video_path"):
            return round(self._SYNC_PER_S * line, 4)
        model = inputs.get("model") or self.NATIVE
        if model == self.NATIVE:
            return round(self._plate_rate(inputs) * self._plate_seconds(seconds), 4)
        if model == self.COMPOSITE:
            return round(self._plate_rate(inputs) * self._plate_seconds(seconds) + self._SYNC_PER_S * line, 4)
        if model in self._MODELS:
            return round(self._MODELS[model][1] * line, 4)
        raise ValueError(f"model must be one of: {self.NATIVE}, {self.COMPOSITE}, "
                         f"{', '.join(self._MODELS)}")

    @staticmethod
    def _upload(path: Path, api_key: str) -> str:
        """Upload a local file to fal.ai storage and return its public URL."""
        import requests

        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        init = requests.post(
            "https://rest.alpha.fal.ai/storage/upload/initiate",
            headers={"Authorization": f"Key {api_key}", "Content-Type": "application/json"},
            json={"content_type": content_type, "file_name": path.name},
            timeout=30,
        )
        init.raise_for_status()
        data = init.json()
        # scale the transfer window with size: a 32 MB video once blew a fixed 120 s timeout
        timeout = max(120, int(path.stat().st_size / (1024 * 1024)) * 20)
        put = requests.put(data["upload_url"], headers={"Content-Type": content_type},
                           data=path.read_bytes(), timeout=timeout)
        put.raise_for_status()
        return data["file_url"]

    # -- queue plumbing ------------------------------------------------------------------
    def _submit(self, endpoint: str, payload: dict, job: dict, api_key: str) -> None:
        """Submit one fal queue request; stores its status/response URLs on ``job``."""
        import requests

        headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
        response = requests.post(f"https://queue.fal.run/{endpoint}", headers=headers,
                                 json=payload, timeout=30)
        response.raise_for_status()
        submitted = response.json()
        job.update({key: submitted[key] for key in ("status_url", "response_url")})
        job["request_id"] = submitted.get("request_id")
        if submitted.get("request_id"):  # every submission's id, for billing reconciliation
            job.setdefault("request_ids", []).append(str(submitted["request_id"]))

    def _await_result(self, job: dict, api_key: str) -> dict:
        """Poll a submitted fal queue request to completion and return its result JSON."""
        import requests
        from urllib.parse import urlparse

        headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
        for field in ("status_url", "response_url"):
            url = urlparse(job[field])
            if url.scheme != "https" or url.netloc != "queue.fal.run":
                raise ValueError("Invalid fal job URL")
        deadline = time.monotonic() + self._MAX_WAIT_SECONDS
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError("fal.ai generation timed out; resume the existing job")
            time.sleep(self._POLL_INTERVAL_SECONDS)
            response = requests.get(job["status_url"], headers=headers, timeout=20)
            response.raise_for_status()
            status = response.json().get("status", "UNKNOWN")
            if status == "COMPLETED":
                break
            if status in {"FAILED", "CANCELLED"}:
                raise RuntimeError(f"fal.ai generation {status.lower()}")
        response = requests.get(job["response_url"], headers=headers, timeout=30)
        if response.status_code >= 400:
            # Surface fal's error body (e.g. content_policy_violation details)
            raise RuntimeError(f"fal.ai result fetch failed ({response.status_code}): {response.text[:800]}")
        return response.json()

    # -- execute -------------------------------------------------------------------------
    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(success=False, error="No fal.ai API key found. " + self.install_instructions)

        if inputs.get("resume_job"):
            job = dict(inputs["resume_job"])
            if job.get("mode") in ("composite", "sync", "native"):
                return self._run_chain(inputs, api_key, resume=job)
            return self._run_job(inputs, api_key, None, job)

        if not (inputs.get("audio_url") or inputs.get("audio_path")):
            return ToolResult(success=False, error="audio_url or audio_path is required")
        if inputs.get("video_url") or inputs.get("video_path"):
            return self._run_chain(inputs, api_key)  # lip-sync fix mode
        if not (inputs.get("image_url") or inputs.get("image_path")):
            return ToolResult(success=False, error="image_url or image_path is required")

        model = inputs.get("model") or self.NATIVE
        if model in (self.NATIVE, self.COMPOSITE):
            return self._run_chain(inputs, api_key)
        if model not in self._MODELS:
            return ToolResult(success=False,
                              error=f"model must be one of: {self.NATIVE}, {self.COMPOSITE}, "
                                    f"{', '.join(self._MODELS)}")

        # -- single-pass avatar models ---------------------------------------------------
        endpoint, per_second = self._MODELS[model]
        try:
            image_url = inputs.get("image_url") or self._upload(Path(inputs["image_path"]), api_key)
            audio_url = inputs.get("audio_url") or self._upload(Path(inputs["audio_path"]), api_key)
        except Exception as exc:
            return ToolResult(success=False,
                              error=f"fal.ai upload failed: {str(exc).replace(api_key, '[REDACTED]')}")

        payload: dict[str, Any] = {"image_url": image_url, "audio_url": audio_url}
        if model == "veed-fabric-1.0":
            payload["resolution"] = inputs.get("resolution") or "720p"
        elif model == "omnihuman-1.5":
            if inputs.get("resolution"):
                payload["resolution"] = inputs["resolution"]
            if inputs.get("turbo_mode") is not None:
                payload["turbo_mode"] = bool(inputs["turbo_mode"])
            if inputs.get("prompt"):
                payload["prompt"] = inputs["prompt"]
        else:  # kling ai-avatar v2
            if inputs.get("prompt"):
                payload["prompt"] = inputs["prompt"]

        job = {
            "tool": self.name,
            "model": model,
            "endpoint": endpoint,
            "per_second_usd": per_second,
            "audio_seconds": self._audio_seconds(inputs),
        }
        return self._run_job(inputs, api_key, payload, job)

    # -- single-pass runner (kept as in v0.1) --------------------------------------------
    def _run_job(self, inputs, api_key, payload, job):
        from tools.provider_jobs import save_job
        from tools.video._shared import probe_output

        import requests

        started = time.time()
        try:
            if job.get("tool") != self.name or (self._MODELS.get(job.get("model")) or ("",))[0] != job.get("endpoint"):
                raise ValueError("Invalid fal avatar resume job")
            if payload is not None:
                self._submit(job["endpoint"], payload, job, api_key)
                save_job(inputs.get("job_path"), job)
            result_data = self._await_result(job, api_key)
            response = requests.get(result_data["video"]["url"], timeout=300)
            response.raise_for_status()
            if not response.content:
                raise ValueError("Empty fal video output")
            output_path = Path(inputs.get("output_path", f"fal_avatar_{job['model']}.mp4"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(response.content)
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"fal.ai avatar generation failed: {str(exc).replace(api_key, '[REDACTED]')}",
                data={"resume_job": job} if job.get("status_url") else {},
                duration_seconds=round(time.time() - started, 2),
            )
        probe = probe_output(output_path)
        billed_seconds = result_data.get("duration") or probe.get("duration_seconds") or job.get("audio_seconds")
        # Fal bills whole output seconds, rounded up (billing events, 2026-11-09).
        cost = round(job["per_second_usd"] * math.ceil(float(billed_seconds)), 4) if billed_seconds else None
        data = {
            "provider": self.provider,
            "model": job["model"],
            "endpoint": job["endpoint"],
            "output": str(output_path),
            "video_url": result_data["video"].get("url"),
            "resume_job": job,
            "request_ids": [str(r) for r in job.get("request_ids") or [] if r],
            "cost_status": "unquoted" if cost is None else "estimated",
            **probe,
        }
        return ToolResult(
            success=True,
            data=data,
            artifacts=[str(output_path)],
            cost_usd=cost,
            duration_seconds=round(time.time() - started, 2),
            model=job["endpoint"],
        )

    # -- composite / lip-sync fix runner -------------------------------------------------
    def _run_chain(self, inputs, api_key, resume: dict | None = None) -> ToolResult:
        from tools.provider_jobs import save_job
        from tools.video._shared import probe_output

        import requests

        started = time.time()
        output_path = Path(inputs.get("output_path", "fal_avatar_h3max.mp4"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        work = output_path.parent
        job: dict[str, Any] = resume or {}
        try:
            if not job:
                # 1) obtain the audio locally, gate it, optionally tighten pauses -----------
                if inputs.get("audio_path"):
                    audio_local = Path(inputs["audio_path"])
                    if not audio_local.exists():
                        raise FileNotFoundError(f"File not found: {audio_local}")
                else:
                    audio_local = work / f".{output_path.stem}.audio"
                    blob = requests.get(inputs["audio_url"], timeout=300)
                    blob.raise_for_status()
                    audio_local.write_bytes(blob.content)
                if inputs.get("trim_silence"):
                    audio_local = self._trim_silence(audio_local, work / f".{output_path.stem}.trimmed.wav")
                peak = self._audio_peak(audio_local)
                if peak is not None and peak < self._MIN_AUDIO_PEAK:
                    raise ValueError(f"the audio is silent (peak {peak:.4f}); refusing to spend on a dead-mouthed video")
                audio_seconds = self._probe_duration(str(audio_local)) or self._audio_seconds(inputs)
                if audio_seconds is None:
                    raise ValueError("cannot determine the audio duration")
                if not (inputs.get("video_url") or inputs.get("video_path")) and audio_seconds > 14.5:
                    raise ValueError(f"audio is {audio_seconds:.1f}s; the acting plate caps at 15s - split the take")
                audio_url = self._upload(audio_local, api_key)
                if inputs.get("video_url") or inputs.get("video_path"):
                    mode = "sync"
                elif (inputs.get("model") or self.NATIVE) == self.NATIVE:
                    mode = "native"  # the plate alone: trimmed to the line, verbatim voice muxed
                else:
                    mode = "composite"
                job = {
                    "tool": self.name, "model": self.NATIVE if mode == "native" else self.COMPOSITE,
                    "mode": mode, "stage": "start",
                    "audio_url": audio_url, "audio_local": str(audio_local),
                    "audio_seconds": round(float(audio_seconds), 3), "costs": {},
                }

            resolution = inputs.get("resolution") or "768P"
            sync_resolution = "480P" if resolution == "480P" else "768P"

            # 2) acting plate conditioned on the audio (composite and native modes) ---------
            if job["mode"] in ("composite", "native") and "plate_video_url" not in job:
                if job.get("stage") != "plate":  # fresh submit (not resuming a pending plate)
                    duration = self._plate_seconds(job["audio_seconds"])
                    job["plate_seconds"] = duration
                    payload = {
                        "prompt": inputs.get("prompt") or "The person talks straight into the camera, natural and "
                                                          "energetic, lips following the words of the soundtrack.",
                        "image_url": inputs.get("image_url") or self._upload(Path(inputs["image_path"]), api_key),
                        "target_audio_url": job["audio_url"],
                        "duration": duration,
                        "resolution": resolution,
                        "prompt_expansion_mode": "disabled",
                    }
                    job["stage"] = "plate"
                    self._submit(self._PLATE_ENDPOINT, payload, job, api_key)
                    save_job(inputs.get("job_path"), job)
                plate_result = self._await_result(job, api_key)
                job["plate_video_url"] = plate_result["video"]["url"]
                # Billed for the length it was asked for, not for the line (see _plate_seconds).
                plate_secs = float(job.get("plate_seconds") or plate_result.get("duration")
                                   or self._plate_seconds(job["audio_seconds"]))
                job["costs"]["plate"] = round(self._plate_rate(inputs) * math.ceil(float(plate_secs)), 4)
                job.pop("status_url", None)
                job.pop("response_url", None)
                save_job(inputs.get("job_path"), job)

            # 3) lip-sync pass (skipped in native mode: the plate's own lips ship) ----------
            if job["mode"] != "native" and "sync_video_url" not in job:
                if job.get("stage") != "sync":
                    source_url = (job.get("plate_video_url") or inputs.get("video_url")
                                  or self._upload(Path(inputs["video_path"]), api_key))
                    payload = {"video_url": source_url, "audio_url": job["audio_url"],
                               "duration_mode": "cut_off", "resolution": sync_resolution}
                    job["stage"] = "sync"
                    self._submit(self._SYNC_ENDPOINT, payload, job, api_key)
                    save_job(inputs.get("job_path"), job)
                sync_result = self._await_result(job, api_key)
                job["sync_video_url"] = sync_result["video"]["url"]
                sync_secs = float(sync_result.get("duration") or job["audio_seconds"])
                job["costs"]["sync"] = round(self._SYNC_PER_S * math.ceil(float(sync_secs)), 4)
                save_job(inputs.get("job_path"), job)

            # 4) download + re-mux the verbatim audio + gate the delivered file -------------
            # Native mode ships the plate itself, trimmed to the line: the plate is generated
            # 0.6s+ long (headroom the sync pass would normally cut), so without a frame-exact
            # -t trim the presenter keeps mouthing after the voice ends.
            source_url = job["plate_video_url"] if job["mode"] == "native" else job["sync_video_url"]
            blob = requests.get(source_url, timeout=300)
            blob.raise_for_status()
            if not blob.content:
                raise ValueError("Empty fal video output")
            synced = work / f".{output_path.stem}.synced.mp4"
            synced.write_bytes(blob.content)
            audio_local = Path(job.get("audio_local", ""))
            if not audio_local.exists():  # resuming in a fresh environment: refetch the audio
                audio_local = work / f".{output_path.stem}.audio"
                blob = requests.get(job["audio_url"], timeout=300)
                blob.raise_for_status()
                audio_local.write_bytes(blob.content)
            trim = float(job["audio_seconds"]) if job["mode"] == "native" else None
            self._mux_audio(synced, audio_local, output_path, trim_seconds=trim)
            synced.unlink(missing_ok=True)
            peak = self._audio_peak(output_path)
            if peak is not None and peak < self._MIN_AUDIO_PEAK:
                raise ValueError("the delivered clip has no audible audio after muxing")
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"fal.ai avatar generation failed: {str(exc).replace(api_key, '[REDACTED]')}",
                data={"resume_job": job} if job.get("stage") else {},
                duration_seconds=round(time.time() - started, 2),
            )
        probe = probe_output(output_path)
        cost = round(sum(job["costs"].values()), 4) if job["costs"] else None
        model_label = {"composite": self.COMPOSITE, "native": self.NATIVE}.get(job["mode"], "h3max-lipsync-fix")
        endpoints = {"composite": [self._PLATE_ENDPOINT, self._SYNC_ENDPOINT],
                     "native": [self._PLATE_ENDPOINT]}.get(job["mode"], [self._SYNC_ENDPOINT])
        data = {
            "provider": self.provider,
            "model": model_label,
            "mode": job["mode"],
            "endpoints": endpoints,
            "output": str(output_path),
            "plate_video_url": job.get("plate_video_url"),
            "video_url": job.get("sync_video_url") or job.get("plate_video_url"),
            "cost_breakdown": job["costs"],
            "resume_job": {k: v for k, v in job.items() if k != "audio_local"},
            "request_ids": [str(r) for r in job.get("request_ids") or [] if r],
            "cost_status": "unquoted" if cost is None else "estimated",
            **probe,
        }
        return ToolResult(
            success=True,
            data=data,
            artifacts=[str(output_path)],
            cost_usd=cost,
            duration_seconds=round(time.time() - started, 2),
            model=self._SYNC_ENDPOINT if job["mode"] == "sync" else self._PLATE_ENDPOINT,
        )
