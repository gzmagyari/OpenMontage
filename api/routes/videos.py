"""Create video jobs. Accepts JSON or multipart (prompt + files)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from api.deps import base_url, ensure_assets_exist, error, require_token
from api.schemas import VideoRequest
from engine.assets import AssetTooLarge
from engine.jobs import JobKind

router = APIRouter(prefix="/v1", tags=["videos"], dependencies=[Depends(require_token)])


async def save_uploads(request: Request, form, roles: list) -> list[dict]:
    """Store each multipart ``files`` upload and return its reference dict (role from ``roles``)."""
    store = request.app.state.assets
    max_bytes = request.app.state.settings.max_upload_mb * 1024 * 1024
    refs: list[dict] = []
    for index, upload in enumerate(form.getlist("files")):
        if not isinstance(upload, StarletteUploadFile):
            continue

        async def chunks(upload: StarletteUploadFile = upload):
            while block := await upload.read(1024 * 1024):
                yield block

        asset = await store.save_stream(chunks(), upload.filename, upload.content_type, max_bytes)
        ref = {"asset_id": asset.id}
        if index < len(roles) and roles[index]:
            ref["role"] = roles[index]
        refs.append(ref)
    return refs


async def _parse_video_request(request: Request) -> VideoRequest:
    ctype = request.headers.get("content-type", "")
    try:
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            body: dict = {"prompt": form.get("prompt")}
            for field in ("options", "references"):
                raw = form.get(field)
                if raw:
                    body[field] = json.loads(str(raw))
            for field in ("idempotency_key", "draft"):
                if form.get(field):
                    body[field] = str(form.get(field))
            roles_raw = form.get("file_roles")
            roles = json.loads(str(roles_raw)) if roles_raw else []
            refs = list(body.get("references") or [])
            refs.extend(await save_uploads(request, form, roles))
            body["references"] = refs
            return VideoRequest.model_validate(body)
        return VideoRequest.model_validate(await request.json())
    except AssetTooLarge as exc:
        raise error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file_too_large", str(exc))
    except (ValidationError, ValueError) as exc:
        raise error(422, "invalid_request", str(exc)[:2000])


_MULTIPART_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string", "description": "What the video should be (required unless draft=true)"},
        "options": {"type": "string", "description": 'JSON-encoded options, e.g. {"duration_seconds": 15, "max_budget_usd": 5}'},
        "references": {"type": "string", "description": 'JSON-encoded list of {"url"|"asset_id", "role", "label"} references'},
        "files": {"type": "array", "items": {"type": "string", "format": "binary"},
                  "description": "Reference files uploaded inline"},
        "file_roles": {"type": "string", "description": 'JSON list of roles for the uploaded files in order, e.g. ["presenter","before","after"]'},
        "draft": {"type": "boolean", "description": "true creates the job without starting it"},
        "idempotency_key": {"type": "string"},
    },
}


@router.post(
    "/videos",
    status_code=status.HTTP_202_ACCEPTED,
    openapi_extra={"requestBody": {"required": True, "content": {
        "application/json": {"schema": VideoRequest.model_json_schema()},
        "multipart/form-data": {"schema": _MULTIPART_SCHEMA},
    }}},
)
async def create_video(request: Request):
    """Prompt (+ optional references) -> finished video. Poll GET /v1/jobs/{id}.

    With ``draft: true`` the job is created but not started: edit it with
    PATCH /v1/jobs/{id}, attach files with POST /v1/jobs/{id}/files, then
    POST /v1/jobs/{id}/start.
    """
    video = await _parse_video_request(request)
    ensure_assets_exist(request, video.references)
    key = request.headers.get("idempotency-key") or video.idempotency_key
    payload = video.model_dump(mode="json", exclude={"idempotency_key", "draft"})
    if video.draft:
        job, created = request.app.state.manager.create_draft(JobKind.VIDEO, payload, idempotency_key=key)
    else:
        job, created = request.app.state.manager.submit(JobKind.VIDEO, payload, idempotency_key=key)
    return JSONResponse(
        job.public(base_url(request)),
        status_code=202 if created else 200,
        headers={"Location": f"/v1/jobs/{job.id}"},
    )
