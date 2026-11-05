"""Shared FastAPI dependencies: auth, app state accessors, error helpers."""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


def error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


# Declared as a security scheme (not a bare header read) so /docs shows the Authorize
# button and marks every protected endpoint with the lock icon.
_bearer = HTTPBearer(auto_error=False, scheme_name="BearerToken",
                     description="The OPENMONTAGE_API_TOKEN value")


async def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    expected = request.app.state.settings.api_token
    if not expected:
        return  # auth disabled (development); startup logs a warning
    token = credentials.credentials.strip() if credentials else ""
    if not token or not hmac.compare_digest(token, expected):
        raise error(status.HTTP_401_UNAUTHORIZED, "unauthorized", "Missing or invalid bearer token")


def base_url(request: Request) -> str:
    return os.environ.get("PUBLIC_BASE_URL") or str(request.base_url)


def ensure_assets_exist(request: Request, references) -> None:
    """422 if any reference names an asset_id the store does not have."""
    for ref in references or []:
        asset_id = ref.get("asset_id") if isinstance(ref, dict) else ref.asset_id
        if asset_id and request.app.state.assets.get(asset_id) is None:
            raise error(422, "unknown_asset", f"Unknown asset_id {asset_id!r}")
