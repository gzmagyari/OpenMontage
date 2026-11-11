"""Shared by the fake SDK streams: what a real tool run leaves behind."""

from __future__ import annotations

from pathlib import Path

from lib import job_ledger


def ledger_dir(options) -> Path:
    """The folder the runner points the session's tools at."""
    return Path(options.env[job_ledger.LEDGER_DIR_ENV])


def record_tool_call(options, tool: str, usd: float, *, model: str | None = None, output: str | None = None,
                     request_ids: list[str] | None = None, note: str | None = None):
    """What the tool layer does around one paid call, in the agent's process."""
    directory = ledger_dir(options)
    call = job_ledger.begin_call(directory, tool=tool, provider="fal.ai",
                                 inputs={"model": model, "output_path": output}, estimate_usd=usd)
    if not call.denied:
        job_ledger.finish_call(directory, call, cost_usd=usd, success=True, request_ids=request_ids, note=note)
    return call
