"""Audio-driven avatar video through fal.ai using the shared FAL credential.

Animates a still photo of a person so they speak a provided audio track
(audio-first workflow: generate narration with a TTS tool, then lip-sync the
photo to it). Wraps the avatar models hosted on fal.ai, so no Kling, HeyGen or
local GPU setup is needed - only FAL_KEY.
"""

from __future__ import annotations

import mimetypes
import os
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


class FalAvatar(BaseTool):
    """Talking-avatar video from one photo + one audio file via fal.ai-hosted models."""

    name = "fal_avatar"
    version = "0.1.0"
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

    capabilities = [
        "photo_to_talking_video",
        "audio_driven_lip_sync",
        "model_selection",
    ]
    supports = {
        "image_to_video": True,
        "audio_input": True,
        "multilingual": True,  # follows whatever language the audio speaks
        "offline": False,
    }
    best_for = [
        "lip-synced presenter takes from a generated or supplied portrait plus TTS narration",
        "keeping one exact voice across every take (the voice is the audio file)",
        "avatar videos with only a fal.ai credential",
    ]
    not_good_for = [
        "clips without speech (use a video generation tool)",
        "full-body acting or camera direction (these models animate the person around the audio)",
    ]

    resume_support = ResumeSupport.FROM_CHECKPOINT

    # model key -> (fal endpoint, USD per output second; fal live pricing API, 2026-11-03)
    _MODELS: dict[str, tuple[str, float]] = {
        "kling-v2-standard": ("fal-ai/kling-video/ai-avatar/v2/standard", 0.0562),
        "kling-v2-pro": ("fal-ai/kling-video/ai-avatar/v2/pro", 0.115),
        "veed-fabric-1.0": ("veed/fabric-1.0", 0.08),
        "omnihuman-1.5": ("fal-ai/bytedance/omnihuman/v1.5", 0.16),
    }
    _POLL_INTERVAL_SECONDS = 5
    _MAX_WAIT_SECONDS = 1200

    input_schema = {
        "type": "object",
        "anyOf": [
            {"required": ["image_url", "audio_url"]},
            {"required": ["image_url", "audio_path"]},
            {"required": ["image_path", "audio_url"]},
            {"required": ["image_path", "audio_path"]},
            {"required": ["resume_job"]},
        ],
        "properties": {
            "image_url": {"type": "string", "description": "Public URL of the portrait to animate"},
            "image_path": {"type": "string", "description": "Local portrait; uploaded to fal storage"},
            "audio_url": {"type": "string", "description": "Public URL of the speech audio to lip-sync"},
            "audio_path": {"type": "string", "description": "Local speech audio; uploaded to fal storage"},
            "model": {
                "type": "string",
                "default": "kling-v2-standard",
                "enum": ["kling-v2-standard", "kling-v2-pro", "veed-fabric-1.0", "omnihuman-1.5"],
                "description": "Avatar model. Prices/s: kling-v2-standard $0.0562, veed-fabric-1.0 $0.08, "
                               "kling-v2-pro $0.115, omnihuman-1.5 $0.16",
            },
            "prompt": {
                "type": "string",
                "description": "Optional motion/performance hint (kling and omnihuman only)",
            },
            "resolution": {
                "type": "string",
                "description": "veed-fabric-1.0: 720p or 480p (default 720p). omnihuman-1.5: 720p or 1080p.",
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
        disk_mb=200,
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
        "model",
        "prompt",
        "resolution",
        "turbo_mode",
    ]
    side_effects = [
        "writes a video file to output_path",
        "may upload the local image/audio to fal.ai storage",
        "submits one paid fal.ai avatar generation request",
    ]
    user_visible_verification = [
        "Watch the clip: lips must follow the audio from the first word, and the face must stay the person's",
    ]

    def _get_api_key(self) -> str | None:
        return os.environ.get("FAL_KEY") or os.environ.get("FAL_AI_API_KEY")

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if self._get_api_key() else ToolStatus.UNAVAILABLE

    def _resolve_model(self, requested: str | None) -> tuple[str, str, float]:
        model = requested or "kling-v2-standard"
        if model not in self._MODELS:
            raise ValueError(f"model must be one of: {', '.join(self._MODELS)}")
        endpoint, per_second = self._MODELS[model]
        return model, endpoint, per_second

    @staticmethod
    def _probe_duration(path: str) -> float | None:
        import shutil as _shutil
        import subprocess

        if not _shutil.which("ffprobe"):
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

    def _audio_seconds(self, inputs: dict[str, Any]) -> float | None:
        if inputs.get("audio_duration_seconds"):
            return float(inputs["audio_duration_seconds"])
        audio_path = inputs.get("audio_path")
        if audio_path and Path(audio_path).exists():
            return self._probe_duration(audio_path)
        return None

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        _, _, per_second = self._resolve_model(inputs.get("model"))
        seconds = self._audio_seconds(inputs)
        if seconds is None:
            raise PriceQuoteRequired(
                "Billing follows the audio length; pass audio_duration_seconds (or a local audio_path) to estimate"
            )
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
        put = requests.put(data["upload_url"], headers={"Content-Type": content_type},
                           data=path.read_bytes(), timeout=120)
        put.raise_for_status()
        return data["file_url"]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(success=False, error="No fal.ai API key found. " + self.install_instructions)

        if inputs.get("resume_job"):
            return self._run_job(inputs, api_key, None, dict(inputs["resume_job"]))

        try:
            model, endpoint, per_second = self._resolve_model(inputs.get("model"))
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))
        if not (inputs.get("image_url") or inputs.get("image_path")):
            return ToolResult(success=False, error="image_url or image_path is required")
        if not (inputs.get("audio_url") or inputs.get("audio_path")):
            return ToolResult(success=False, error="audio_url or audio_path is required")

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

        audio_seconds = self._audio_seconds(inputs)
        job = {
            "tool": self.name,
            "model": model,
            "endpoint": endpoint,
            "per_second_usd": per_second,
            "audio_seconds": audio_seconds,
        }
        return self._run_job(inputs, api_key, payload, job)

    def _run_job(self, inputs, api_key, payload, job):
        import requests
        from urllib.parse import urlparse

        from tools.provider_jobs import save_job
        from tools.video._shared import probe_output

        started = time.time()
        headers = {"Authorization": f"Key {api_key}", "Content-Type": "application/json"}
        try:
            if job.get("tool") != self.name or (self._MODELS.get(job.get("model")) or ("",))[0] != job.get("endpoint"):
                raise ValueError("Invalid fal avatar resume job")
            if payload is not None:
                response = requests.post(f"https://queue.fal.run/{job['endpoint']}",
                                         headers=headers, json=payload, timeout=30)
                response.raise_for_status()
                submitted = response.json()
                job.update({key: submitted[key] for key in ("status_url", "response_url")})
                job["request_id"] = submitted.get("request_id")
                save_job(inputs.get("job_path"), job)
            for field in ("status_url", "response_url"):
                url = urlparse(job[field])
                if url.scheme != "https" or url.netloc != "queue.fal.run":
                    raise ValueError("Invalid fal job URL")
            deadline = time.monotonic() + self._MAX_WAIT_SECONDS
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("fal.ai avatar generation timed out; resume the existing job")
                time.sleep(self._POLL_INTERVAL_SECONDS)
                response = requests.get(job["status_url"], headers=headers, timeout=20)
                response.raise_for_status()
                status = response.json().get("status", "UNKNOWN")
                if status == "COMPLETED":
                    break
                if status in {"FAILED", "CANCELLED"}:
                    raise RuntimeError(f"fal.ai avatar generation {status.lower()}")
            response = requests.get(job["response_url"], headers=headers, timeout=30)
            if response.status_code >= 400:
                # Surface fal's error body (e.g. content_policy_violation details)
                raise RuntimeError(f"fal.ai avatar result fetch failed ({response.status_code}): "
                                   f"{response.text[:800]}")
            result_data = response.json()
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
