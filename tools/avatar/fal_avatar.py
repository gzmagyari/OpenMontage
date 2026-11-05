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
    _PLATE_ENDPOINT = "minimax/h3-max/image-to-video"          # $0.035/s
    _SYNC_ENDPOINT = "minimax/h3-max/lip-sync/video-to-video"  # $0.05/s
    _PLATE_PER_S = 0.035
    _SYNC_PER_S = 0.05
    _POLL_INTERVAL_SECONDS = 5
    _MAX_WAIT_SECONDS = 1200
    _MIN_AUDIO_PEAK = 0.005  # below this the track is treated as silent and refused

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
                "default": "h3max-acting",
                "enum": ["h3max-acting", "kling-v2-standard", "kling-v2-pro", "veed-fabric-1.0", "omnihuman-1.5"],
                "description": "h3max-acting = prompted acting + accurate lips (H3 Max plate conditioned on the audio, "
                               "then an H3 Max lip-sync pass; ~$0.085/s, ~2 min). Single-pass avatars: "
                               "kling-v2-standard $0.0562/s, veed-fabric-1.0 $0.08/s, kling-v2-pro $0.115/s, "
                               "omnihuman-1.5 $0.16/s",
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
    def _mux_audio(video: Path, audio: Path, dst: Path) -> Path:
        """Replace the video's soundtrack with ``audio`` verbatim (stream-copies the video)."""
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(audio),
                        "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-shortest", str(dst)], check=True, timeout=300)
        return dst

    def _audio_seconds(self, inputs: dict[str, Any]) -> float | None:
        if inputs.get("audio_duration_seconds"):
            return float(inputs["audio_duration_seconds"])
        audio_path = inputs.get("audio_path")
        if audio_path and Path(audio_path).exists():
            return self._probe_duration(audio_path)
        return None

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        seconds = self._audio_seconds(inputs)
        if seconds is None:
            raise PriceQuoteRequired(
                "Billing follows the audio length; pass audio_duration_seconds (or a local audio_path) to estimate"
            )
        if inputs.get("video_url") or inputs.get("video_path"):
            per_second = self._SYNC_PER_S
        else:
            model = inputs.get("model") or self.COMPOSITE
            if model == self.COMPOSITE:
                per_second = self._PLATE_PER_S + self._SYNC_PER_S
            elif model in self._MODELS:
                per_second = self._MODELS[model][1]
            else:
                raise ValueError(f"model must be one of: {self.COMPOSITE}, {', '.join(self._MODELS)}")
        return round(per_second * seconds, 4)

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
            if job.get("mode") in ("composite", "sync"):
                return self._run_chain(inputs, api_key, resume=job)
            return self._run_job(inputs, api_key, None, job)

        if not (inputs.get("audio_url") or inputs.get("audio_path")):
            return ToolResult(success=False, error="audio_url or audio_path is required")
        if inputs.get("video_url") or inputs.get("video_path"):
            return self._run_chain(inputs, api_key)  # lip-sync fix mode
        if not (inputs.get("image_url") or inputs.get("image_path")):
            return ToolResult(success=False, error="image_url or image_path is required")

        model = inputs.get("model") or self.COMPOSITE
        if model == self.COMPOSITE:
            return self._run_chain(inputs, api_key)
        if model not in self._MODELS:
            return ToolResult(success=False,
                              error=f"model must be one of: {self.COMPOSITE}, {', '.join(self._MODELS)}")

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
        cost = round(job["per_second_usd"] * float(billed_seconds), 4) if billed_seconds else None
        data = {
            "provider": self.provider,
            "model": job["model"],
            "endpoint": job["endpoint"],
            "output": str(output_path),
            "video_url": result_data["video"].get("url"),
            "resume_job": job,
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
                mode = "sync" if (inputs.get("video_url") or inputs.get("video_path")) else "composite"
                job = {
                    "tool": self.name, "model": self.COMPOSITE, "mode": mode, "stage": "start",
                    "audio_url": audio_url, "audio_local": str(audio_local),
                    "audio_seconds": round(float(audio_seconds), 3), "costs": {},
                }

            resolution = inputs.get("resolution") or "768P"
            sync_resolution = "480P" if resolution == "480P" else "768P"

            # 2) acting plate conditioned on the audio (composite mode only) ---------------
            if job["mode"] == "composite" and "plate_video_url" not in job:
                if job.get("stage") != "plate":  # fresh submit (not resuming a pending plate)
                    duration = min(15, max(2, math.ceil(job["audio_seconds"] + 0.6)))
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
                plate_secs = float(plate_result.get("duration") or job["audio_seconds"])
                job["costs"]["plate"] = round(self._PLATE_PER_S * plate_secs, 4)
                job.pop("status_url", None)
                job.pop("response_url", None)
                save_job(inputs.get("job_path"), job)

            # 3) lip-sync pass -------------------------------------------------------------
            if "sync_video_url" not in job:
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
                job["costs"]["sync"] = round(self._SYNC_PER_S * sync_secs, 4)
                save_job(inputs.get("job_path"), job)

            # 4) download + re-mux the verbatim audio + gate the delivered file -------------
            blob = requests.get(job["sync_video_url"], timeout=300)
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
            self._mux_audio(synced, audio_local, output_path)
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
        data = {
            "provider": self.provider,
            "model": self.COMPOSITE if job["mode"] == "composite" else "h3max-lipsync-fix",
            "mode": job["mode"],
            "endpoints": [self._PLATE_ENDPOINT, self._SYNC_ENDPOINT] if job["mode"] == "composite"
                         else [self._SYNC_ENDPOINT],
            "output": str(output_path),
            "plate_video_url": job.get("plate_video_url"),
            "video_url": job.get("sync_video_url"),
            "cost_breakdown": job["costs"],
            "resume_job": {k: v for k, v in job.items() if k != "audio_local"},
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
