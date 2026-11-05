"""Job polling, listing, cancellation, file download and revision."""

from __future__ import annotations

import json
import mimetypes
import re

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse

from api.deps import base_url, error, require_token
from api.schemas import ReviseRequest
from engine.jobs import JobKind, JobStatus

router = APIRouter(prefix="/v1", tags=["jobs"], dependencies=[Depends(require_token)])

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")


def _job_or_404(request: Request, job_id: str):
    job = request.app.state.store.get(job_id)
    if job is None:
        raise error(status.HTTP_404_NOT_FOUND, "job_not_found", f"No job {job_id!r}")
    return job


@router.get("/jobs")
async def list_jobs(
    request: Request,
    status_filter: JobStatus | None = Query(None, alias="status"),
    kind: JobKind | None = None,
    limit: int = Query(50, ge=1, le=500),
):
    jobs = request.app.state.store.list(status=status_filter, kind=kind, limit=limit)
    root = base_url(request)
    return {"jobs": [job.public(root) for job in jobs]}


def _read_journal(path, offset: int, limit: int) -> dict:
    """The agent's chat stream: journal events [offset, offset+limit) plus the next offset."""
    events: list[dict] = []
    total = 0
    try:
        with open(path, encoding="utf-8") as fh:
            for index, line in enumerate(fh):
                total = index + 1
                if index < offset or len(events) >= limit:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    events.append({"kind": "malformed"})
    except OSError:
        return {"events": [], "next_offset": offset, "total": 0}
    return {"events": events, "next_offset": offset + len(events), "total": total}


@router.get("/jobs/{job_id}")
async def get_job(
    request: Request,
    job_id: str,
    transcript_offset: int = Query(0, ge=0, description="First chat-stream event to return"),
    transcript_limit: int = Query(200, ge=0, le=1000, description="0 omits the chat stream"),
):
    job = _job_or_404(request, job_id)
    data = job.public(base_url(request))
    if transcript_limit > 0:
        journal = request.app.state.store.job_dir(job_id) / "agent" / "journal.jsonl"
        if journal.is_file():
            data["transcript"] = _read_journal(journal, transcript_offset, transcript_limit)
    return data


@router.delete("/jobs/{job_id}")
async def cancel_job(request: Request, job_id: str):
    _job_or_404(request, job_id)
    job = request.app.state.manager.cancel(job_id)
    return job.public(base_url(request))


@router.get("/jobs/{job_id}/files")
async def list_job_files(request: Request, job_id: str):
    _job_or_404(request, job_id)
    files_dir = request.app.state.store.files_dir(job_id)
    root = base_url(request).rstrip("/")
    files = []
    if files_dir.exists():
        for path in sorted(files_dir.iterdir()):
            if path.is_file():
                files.append({
                    "name": path.name,
                    "size_bytes": path.stat().st_size,
                    "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                    "url": f"{root}/v1/jobs/{job_id}/files/{path.name}",
                })
    return {"files": files}


@router.get("/jobs/{job_id}/files/{name}")
async def download_job_file(request: Request, job_id: str, name: str):
    _job_or_404(request, job_id)
    if not _SAFE_NAME.match(name):
        raise error(status.HTTP_400_BAD_REQUEST, "invalid_name", "Invalid file name")
    path = request.app.state.store.files_dir(job_id) / name
    if not path.is_file():
        raise error(status.HTTP_404_NOT_FOUND, "file_not_found", f"No file {name!r} for job {job_id!r}")
    return FileResponse(path, media_type=mimetypes.guess_type(name)[0] or "application/octet-stream", filename=name)


@router.post("/jobs/{job_id}/revise", status_code=status.HTTP_202_ACCEPTED)
async def revise_job(request: Request, job_id: str, body: ReviseRequest):
    parent = _job_or_404(request, job_id)
    if parent.kind not in (JobKind.VIDEO, JobKind.REVISE) or parent.status != JobStatus.COMPLETED:
        raise error(status.HTTP_409_CONFLICT, "not_revisable", "Only completed video jobs can be revised")
    payload = {"parent_job_id": job_id, "feedback": body.feedback,
               "options": body.options.model_dump(exclude_unset=True) if body.options else None}
    key = request.headers.get("idempotency-key") or body.idempotency_key
    job, created = request.app.state.manager.submit(JobKind.REVISE, payload, idempotency_key=key, parent_job_id=job_id)
    return JSONResponse(job.public(base_url(request)), status_code=202 if created else 200,
                        headers={"Location": f"/v1/jobs/{job.id}"})
