"""Reference uploads: upload once, reference by asset_id in any request."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from fastapi.responses import FileResponse

from api.deps import error, require_token
from engine.assets import AssetTooLarge

router = APIRouter(prefix="/v1", tags=["assets"], dependencies=[Depends(require_token)])


@router.post("/assets", status_code=status.HTTP_201_CREATED)
async def upload_asset(request: Request, file: UploadFile = File(...)):
    max_bytes = request.app.state.settings.max_upload_mb * 1024 * 1024

    async def chunks():
        while block := await file.read(1024 * 1024):
            yield block

    try:
        asset = await request.app.state.assets.save_stream(chunks(), file.filename, file.content_type, max_bytes)
    except AssetTooLarge as exc:
        raise error(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file_too_large", str(exc))
    return asset.model_dump(mode="json")


@router.get("/assets/{asset_id}")
async def get_asset(request: Request, asset_id: str):
    asset = request.app.state.assets.get(asset_id)
    if asset is None:
        raise error(status.HTTP_404_NOT_FOUND, "asset_not_found", f"No asset {asset_id!r}")
    return asset.model_dump(mode="json")


@router.get("/assets/{asset_id}/content")
async def get_asset_content(request: Request, asset_id: str):
    store = request.app.state.assets
    asset = store.get(asset_id)
    if asset is None:
        raise error(status.HTTP_404_NOT_FOUND, "asset_not_found", f"No asset {asset_id!r}")
    return FileResponse(store.path(asset_id), media_type=asset.content_type, filename=asset.filename)
