"""Job runners.

TOOL_RUN executes one registry tool as a job (internal API). VIDEO and REVISE
run OpenMontage's standard agent-driven flow unattended (engine/agent): one
Claude Agent SDK session per job, journaled as the job's chat stream.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import shutil
from pathlib import Path
from typing import Any

from engine.jobs.context import JobContext, JobFailed
from engine.jobs.models import JobKind


def _file_entry(path: Path) -> dict[str, Any]:
    return {"name": path.name, "size_bytes": path.stat().st_size,
            "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _looks_like_server_path(value: str) -> bool:
    if not value or len(value) > 500 or "\n" in value:
        return False
    from lib.paths import REPO_ROOT

    candidate = Path(value)
    if candidate.is_absolute() or value.startswith("~"):
        return True
    try:
        return (REPO_ROOT / candidate).exists() or (Path.cwd() / candidate).exists()
    except (OSError, ValueError):
        return False


def _resolve_tool_inputs(ctx: JobContext, tool: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    assets = ctx.services.assets
    out_dir = ctx.work_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    def resolve(value: Any, key: str | None) -> Any:
        if isinstance(value, dict):
            return {k: resolve(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, key) for v in value]
        if isinstance(value, str):
            if value.startswith("asset:"):
                asset_id = value[len("asset:"):]
                if assets.get(asset_id) is None:
                    raise JobFailed("unknown_asset", f"Unknown asset {asset_id!r} in input {key!r}")
                return str(assets.path(asset_id))
            if _looks_like_server_path(value):
                raise JobFailed("invalid_request", f"Input {key!r} looks like a server file path. Upload files with "
                                                   "POST /v1/assets and pass 'asset:<id>' instead.")
        return value

    resolved = resolve(dict(inputs), None)
    props = (getattr(tool, "input_schema", None) or {}).get("properties", {}) or {}
    for key in props:
        lk = key.lower()
        if "output" not in lk:
            continue
        if lk.endswith("dir"):
            resolved[key] = str(out_dir)
        elif lk.endswith("path") and resolved.get(key):
            from engine.assets import safe_filename

            resolved[key] = str(out_dir / safe_filename(Path(str(resolved[key])).name, "output"))
    return resolved


async def run_tool(ctx: JobContext) -> dict[str, Any]:
    from engine.assets import safe_filename
    from tools.tool_registry import registry

    req = ctx.job.request
    name = req["tool"]
    await asyncio.to_thread(registry.ensure_discovered)
    tool = registry.get(name)
    if tool is None:
        raise JobFailed("tool_not_found", f"No tool {name!r}")
    inputs = _resolve_tool_inputs(ctx, tool, req.get("inputs") or {})
    ctx.set_stage("running", f"running {name}", progress=10)
    async with ctx.resource("tool", f"tool {name}"):
        result = await asyncio.to_thread(tool.execute, inputs)
    if not result.success:
        raise JobFailed("tool_failed", (result.error or f"{name} failed")[:2000])
    if result.cost_usd:
        ctx.add_cost(f"tool {name}", float(result.cost_usd), provider=getattr(tool, "provider", None), estimated=True)
    files: dict[str, dict[str, Any]] = {}
    for i, artifact in enumerate(result.artifacts or [], 1):
        path = Path(str(artifact))
        if path.is_file() and _inside(path, ctx.job_dir):
            dest = ctx.files_dir / safe_filename(path.name, f"artifact_{i}")
            if dest.resolve() != path.resolve():
                shutil.copy2(path, dest)
            files[f"artifact_{i}"] = _file_entry(dest)
    data = json.loads(json.dumps(result.data or {}, default=str))
    return {"tool": name, "data": data, "files": files, "cost_usd": result.cost_usd,
            "seconds": result.duration_seconds}


def build_runners() -> dict[JobKind, Any]:
    from engine.agent import run_revise, run_video

    return {JobKind.TOOL_RUN: run_tool, JobKind.VIDEO: run_video, JobKind.REVISE: run_revise}


__all__ = ["build_runners", "run_tool"]
