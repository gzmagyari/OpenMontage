"""Health, readiness and capabilities."""

from __future__ import annotations

import os
import shutil

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.deps import require_token
from engine import __version__

public = APIRouter(tags=["meta"])
router = APIRouter(prefix="/v1", tags=["meta"], dependencies=[Depends(require_token)])


@public.get("/healthz")
async def healthz():
    return {"status": "ok", "version": __version__}


@public.get("/readyz")
async def readyz(request: Request):
    settings = request.app.state.settings
    checks = {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "node": shutil.which("node") is not None,
        "fal_key": bool(os.environ.get("FAL_KEY") or os.environ.get("FAL_AI_API_KEY")),
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "data_dir_writable": os.access(settings.data_dir, os.W_OK),
    }
    ok = all(checks.values())
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)


@router.get("/capabilities")
async def capabilities(request: Request):
    settings = request.app.state.settings
    return {
        "version": __version__,
        "duration_seconds": {"min": 10, "max": 60},
        "output": {"width": 1080, "height": 1920, "fps": 30, "codec": "h264"},
        "tool_api_enabled": settings.enable_tool_api,
        "limits": request.app.state.manager.stats(),
    }


@router.get("/stats")
async def stats(request: Request):
    return request.app.state.manager.stats()
