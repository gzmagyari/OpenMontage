"""Internal raw-tool API (enabled with OPENMONTAGE_ENABLE_TOOL_API=1).

Every registry tool can be run as a job. File inputs are passed as "asset:<asset_id>"
strings; outputs are forced into the job's own folder and exposed via /v1/jobs/{id}/files.
Tools that execute arbitrary code, workflows or scripts are deny-listed.
"""

from __future__ import annotations

import asyncio
import os

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from api.deps import base_url, error, require_token
from api.schemas import ToolRunRequest
from engine.jobs import JobKind

router = APIRouter(prefix="/v1", tags=["tools"], dependencies=[Depends(require_token)])

DENYLIST = {
    "blender_world",      # runs Blender Python
    "comfyui_image",      # arbitrary ComfyUI workflow graphs
    "comfyui_video",
    "comfyui_music",
    "math_animate",       # executes Manim code
    "hyperframes_compose",  # renders arbitrary HTML/JS projects
    "video_compose",      # atelier mode renders arbitrary TSX entries
    "bgm_montage_lite",   # runs an external checkout
    "threejs_world",      # renders arbitrary project folders
    "screen_recorder",    # captures the host screen
    "cap_recorder",
}


def denylist() -> set[str]:
    extra = {t.strip() for t in os.environ.get("OPENMONTAGE_TOOL_DENYLIST", "").split(",") if t.strip()}
    return DENYLIST | extra


async def _registry():
    from tools.tool_registry import registry

    await asyncio.to_thread(registry.ensure_discovered)  # imports ~150 modules once; keep the loop free
    return registry


def _describe(tool) -> dict:
    return {
        "name": tool.name,
        "capability": getattr(tool, "capability", None),
        "provider": getattr(tool, "provider", None),
        "status": tool.get_status().value,
        "runtime": str(getattr(tool, "runtime", "")).split(".")[-1].lower(),
        "stability": str(getattr(tool, "stability", "")).split(".")[-1].lower(),
        "input_schema": getattr(tool, "input_schema", {}),
    }


@router.get("/tools")
async def list_tools():
    reg = await _registry()
    blocked = denylist()
    return {"tools": [_describe(t) for name, t in sorted(reg._tools.items()) if name not in blocked]}


@router.get("/tools/{name}")
async def get_tool(name: str):
    reg = await _registry()
    tool = reg._tools.get(name)
    if tool is None or name in denylist():
        raise error(status.HTTP_404_NOT_FOUND, "tool_not_found", f"No runnable tool {name!r}")
    info = _describe(tool)
    info["install_instructions"] = getattr(tool, "install_instructions", None)
    return info


@router.post("/tools/{name}/runs", status_code=status.HTTP_202_ACCEPTED)
async def run_tool(request: Request, name: str, body: ToolRunRequest):
    reg = await _registry()
    if name not in reg._tools or name in denylist():
        raise error(status.HTTP_404_NOT_FOUND, "tool_not_found", f"No runnable tool {name!r}")
    key = request.headers.get("idempotency-key") or body.idempotency_key
    job, created = request.app.state.manager.submit(JobKind.TOOL_RUN, {"tool": name, "inputs": body.inputs}, idempotency_key=key)
    return JSONResponse(job.public(base_url(request)), status_code=202 if created else 200,
                        headers={"Location": f"/v1/jobs/{job.id}"})
