"""Shared by the fake SDK streams: what a real tool run leaves behind."""

from __future__ import annotations

from pathlib import Path

from lib import job_ledger


POST = {"title": "Cold brew without the wait", "description": "Lumabrew makes cold brew in a minute. See how.",
        "hashtags": ["lumabrew", "coldbrew", "coffeetok", "morningroutine"]}


def write_posting(project: Path, *, post: bool = True, cover: bool = True,
                  cover_at: float | None = 1.6, cover_of: str = "video.mp4") -> None:
    """What a real run leaves beside its export: the cover picture and the post text. Call it after
    the export is written: a cover older than the video counts as out of date. `kit cover` also
    records which second of which export the frame is (`cover_at`, `cover_of`); None leaves the
    record out, as when a cover was made by other means."""
    import json

    if post:
        (project / "artifacts").mkdir(parents=True, exist_ok=True)
        (project / "artifacts" / "post.json").write_text(json.dumps(POST), encoding="utf-8")
    if cover:
        (project / "exports").mkdir(parents=True, exist_ok=True)
        (project / "exports" / "cover.jpg").write_bytes(b"\xff\xd8\xff fake cover")
        record = project / "work" / "kit" / "cover.json"
        if cover_at is not None:
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text(json.dumps({"file": "exports/cover.jpg", "at": cover_at, "video": cover_of}),
                              encoding="utf-8")
        else:
            record.unlink(missing_ok=True)


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
