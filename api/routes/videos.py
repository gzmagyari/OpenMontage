"""Create video jobs. Accepts JSON or multipart (prompt + files)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile as StarletteUploadFile

from api.deps import base_url, error, require_token
from api.schemas import VideoRequest
from engine.assets import AssetTooLarge
from engine.jobs import JobKind

router = APIRouter(prefix="/v1", tags=["videos"], dependencies=[Depends(require_token)])


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
            if form.get("idempotency_key"):
                body["idempotency_key"] = str(form.get("idempotency_key"))
            roles_raw = form.get("file_roles")
            roles = json.loads(str(roles_raw)) if roles_raw else []
            store = request.app.state.assets
            max_bytes = request.app.state.settings.max_upload_mb * 1024 * 1024
            refs = list(body.get("references") or [])
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
            body["references"] = refs
            return VideoRequest.model_validate(body)
        return VideoRequest.model_validate(await request.json())
    except AssetTooLarge as exc:
        raise error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file_too_large", str(exc))
    except (ValidationError, ValueError) as exc:
        raise error(422, "invalid_request", str(exc)[:2000])


def _check_assets(request: Request, video: VideoRequest) -> None:
    for ref in video.references:
        if ref.asset_id and request.app.state.assets.get(ref.asset_id) is None:
            raise error(422, "unknown_asset", f"Unknown asset_id {ref.asset_id!r}")


@router.post("/videos", status_code=status.HTTP_202_ACCEPTED)
async def create_video(request: Request):
    """Prompt (+ optional references) -> finished video. Poll GET /v1/jobs/{id}."""
    video = await _parse_video_request(request)
    _check_assets(request, video)
    key = request.headers.get("idempotency-key") or video.idempotency_key
    job, created = request.app.state.manager.submit(
        JobKind.VIDEO, video.model_dump(exclude={"idempotency_key"}), idempotency_key=key)
    return JSONResponse(
        job.public(base_url(request)),
        status_code=202 if created else 200,
        headers={"Location": f"/v1/jobs/{job.id}"},
    )
