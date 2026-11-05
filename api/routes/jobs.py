"""Job polling, listing, cancellation, file download and revision."""

from __future__ import annotations

import json
import mimetypes
import re

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse

from api.deps import base_url, ensure_assets_exist, error, require_token
from api.schemas import ReviseRequest, VideoPatch, VideoRequest
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


_JOB_EXAMPLE = {
    "id": "job_6cd2759bb9816f37", "kind": "video", "status": "running",
    "stage": "talking shots", "stage_detail": "fal_avatar", "progress": 40, "queue_position": None,
    "created_at": "2026-11-05T12:00:00Z", "started_at": "2026-11-05T12:00:01Z",
    "updated_at": "2026-11-05T12:03:40Z", "finished_at": None, "parent_job_id": None,
    "cost": {"estimated_usd": None, "spent_usd": 0.23,
             "lines": [{"item": "claude agent", "usd": 0.23, "provider": "anthropic", "estimated": False}]},
    "error": None, "result": None, "warnings": [], "attempts": 1,
    "transcript": {"events": [{"kind": "assistant", "text": "Generating the voiceover next."}],
                   "next_offset": 42, "total": 42},
}


@router.get(
    "/jobs/{job_id}",
    responses={200: {"description": "Job state. `result.files` appears on completion; drafts also "
                                    "include `request` (the editable prompt/options/references). "
                                    "`transcript` is the agent chat stream (page via `transcript_offset`).",
                     "content": {"application/json": {"example": _JOB_EXAMPLE}}}},
)
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


def _draft_or_409(request: Request, job_id: str):
    job = _job_or_404(request, job_id)
    if job.status != JobStatus.DRAFT:
        raise error(status.HTTP_409_CONFLICT, "not_a_draft",
                    f"Job {job_id!r} is {job.status.value}; only draft jobs can be edited")
    return job


@router.patch("/jobs/{job_id}")
async def update_draft(request: Request, job_id: str, body: VideoPatch):
    """Edit a draft job: prompt, options, and/or references (the list is replaced wholesale)."""
    job = _draft_or_409(request, job_id)
    changes = body.model_dump(mode="json", exclude_unset=True)
    if body.references is not None:
        ensure_assets_exist(request, body.references)
    job.request.update({key: value for key, value in changes.items() if value is not None})
    request.app.state.store.save(job, force=True)
    return job.public(base_url(request))


@router.post("/jobs/{job_id}/files")
async def upload_draft_files(request: Request, job_id: str):
    """Upload reference files straight onto a draft job (multipart ``files`` + optional ``file_roles``).

    Each file lands in the shared asset store (de-duplicated) and is appended to the
    draft's references.
    """
    from api.routes.videos import save_uploads
    from engine.assets import AssetTooLarge

    job = _draft_or_409(request, job_id)
    form = await request.form()
    try:
        roles_raw = form.get("file_roles")
        roles = json.loads(str(roles_raw)) if roles_raw else []
        refs = await save_uploads(request, form, roles)
    except AssetTooLarge as exc:
        raise error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file_too_large", str(exc))
    except ValueError as exc:
        raise error(422, "invalid_request", str(exc)[:2000])
    if not refs:
        raise error(422, "no_files", "Send at least one multipart 'files' part")
    existing = list(job.request.get("references") or [])
    if len(existing) + len(refs) > 12:
        raise error(422, "too_many_references", "A job holds at most 12 references")
    job.request["references"] = existing + refs
    request.app.state.store.save(job, force=True)
    return job.public(base_url(request))


@router.post("/jobs/{job_id}/start", status_code=status.HTTP_202_ACCEPTED)
async def start_job(request: Request, job_id: str):
    """Validate the assembled draft and queue it. Idempotent for already-started jobs."""
    job = _job_or_404(request, job_id)
    if job.status != JobStatus.DRAFT:
        if job.status in (JobStatus.CANCELLED, JobStatus.FAILED):
            raise error(status.HTTP_409_CONFLICT, "not_startable",
                        f"Job {job_id!r} is {job.status.value} and cannot be started")
        # Already queued/running/completed: a retried start call is a no-op.
        return JSONResponse(job.public(base_url(request)), status_code=200,
                            headers={"Location": f"/v1/jobs/{job.id}"})
    try:
        video = VideoRequest.model_validate({k: v for k, v in job.request.items() if k != "parent_job_id"})
    except Exception as exc:
        raise error(422, "invalid_request", f"Draft is not ready to start: {str(exc)[:1500]}")
    ensure_assets_exist(request, video.references)
    job = request.app.state.manager.start_draft(job_id)
    return JSONResponse(job.public(base_url(request)), status_code=202,
                        headers={"Location": f"/v1/jobs/{job.id}"})


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
