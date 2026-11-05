"""Shared FastAPI dependencies: auth, app state accessors, error helpers."""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request, status


def error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


async def require_token(request: Request) -> None:
    expected = request.app.state.settings.api_token
    if not expected:
        return  # auth disabled (development); startup logs a warning
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), expected):
        raise error(status.HTTP_401_UNAUTHORIZED, "unauthorized", "Missing or invalid bearer token")


def base_url(request: Request) -> str:
    return os.environ.get("PUBLIC_BASE_URL") or str(request.base_url)
