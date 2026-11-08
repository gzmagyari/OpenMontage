"""Health, readiness and capabilities."""

from __future__ import annotations

import asyncio
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
    problems = {}
    if getattr(settings, "agent_sandbox", "off") == "required":
        # Video jobs need a box, so a host that can't build one isn't ready. A deploy sees it
        # here without running anything in the container.
        from engine.sandbox.box import sandbox_problem  # engine.sandbox imports engine.agent

        problem = await asyncio.to_thread(sandbox_problem)
        checks["agent_box"] = problem is None
        if problem:
            problems["agent_box"] = problem
    ok = all(checks.values())
    body = {"ready": ok, "checks": checks}
    if problems:
        body["problems"] = problems
    return JSONResponse(body, status_code=200 if ok else 503)


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
