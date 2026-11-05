"""Reference asset store: uploaded or downloaded files, de-duplicated by SHA-256."""

from __future__ import annotations

import hashlib
import ipaddress
import mimetypes
import re
import shutil
import socket
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from engine.jobs.models import new_id, utcnow
from engine.jobs.store import atomic_write_text

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}


class AssetTooLarge(Exception):
    pass


class UnsafeURL(Exception):
    pass


class Asset(BaseModel):
    id: str
    filename: str
    content_type: str
    kind: str  # image | video | audio | other
    size_bytes: int
    sha256: str
    created_at: datetime = Field(default_factory=utcnow)
    source: str = "upload"  # upload | url
    source_url: str | None = None


def safe_filename(name: str | None, fallback: str = "file") -> str:
    base = Path(name or fallback).name
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or fallback
    return base[:120]


def detect_kind(filename: str, content_type: str | None) -> tuple[str, str]:
    ext = Path(filename).suffix.lower()
    ctype = (content_type or "").split(";")[0].strip().lower() or (mimetypes.guess_type(filename)[0] or "")
    if ext in IMAGE_EXT or ctype.startswith("image/"):
        return "image", ctype or "image/jpeg"
    if ext in VIDEO_EXT or ctype.startswith("video/"):
        return "video", ctype or "video/mp4"
    if ext in AUDIO_EXT or ctype.startswith("audio/"):
        return "audio", ctype or "audio/mpeg"
    return "other", ctype or "application/octet-stream"


def check_public_url(url: str, allow_private: bool = False) -> None:
    """Reject non-http(s) URLs and (by default) hosts resolving to private/loopback ranges (SSRF guard)."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURL(f"Only http(s) URLs are allowed: {url!r}")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURL(f"Cannot resolve host {parsed.hostname!r}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
            raise UnsafeURL(f"URL host {parsed.hostname!r} resolves to a non-public address")


class AssetStore:
    def __init__(self, assets_dir: Path):
        self.dir = assets_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._by_id: dict[str, Asset] = {}
        self._by_sha: dict[str, str] = {}
        for meta in self.dir.glob("*/meta.json"):
            try:
                asset = Asset.model_validate_json(meta.read_text(encoding="utf-8"))
            except Exception:
                continue
            self._by_id[asset.id] = asset
            self._by_sha[asset.sha256] = asset.id

    def get(self, asset_id: str) -> Asset | None:
        return self._by_id.get(asset_id)

    def path(self, asset_id: str) -> Path:
        asset = self._by_id[asset_id]
        return self.dir / asset.id / asset.filename

    async def save_stream(
        self,
        chunks: AsyncIterator[bytes],
        filename: str | None,
        content_type: str | None,
        max_bytes: int,
        source: str = "upload",
        source_url: str | None = None,
    ) -> Asset:
        """Stream bytes to disk (hashing as we go), enforce the size cap, de-duplicate by content."""
        name = safe_filename(filename)
        staging = self.dir / f".incoming-{new_id('tmp')}"
        sha = hashlib.sha256()
        size = 0
        try:
            with open(staging, "wb") as fh:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > max_bytes:
                        raise AssetTooLarge(f"File exceeds the {max_bytes // (1024 * 1024)} MB limit")
                    sha.update(chunk)
                    fh.write(chunk)
            digest = sha.hexdigest()
            existing = self._by_sha.get(digest)
            if existing and existing in self._by_id:
                return self._by_id[existing]
            kind, ctype = detect_kind(name, content_type)
            if "." not in name:
                ext = mimetypes.guess_extension(ctype) or ""
                name = f"{name}{ext}"
            asset = Asset(
                id=new_id("ast"),
                filename=name,
                content_type=ctype,
                kind=kind,
                size_bytes=size,
                sha256=digest,
                source=source,
                source_url=source_url,
            )
            target_dir = self.dir / asset.id
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(staging), target_dir / asset.filename)
            atomic_write_text(target_dir / "meta.json", asset.model_dump_json(indent=2))
            self._by_id[asset.id] = asset
            self._by_sha[digest] = asset.id
            return asset
        finally:
            if staging.exists():
                staging.unlink(missing_ok=True)

    async def save_path(self, path: Path, filename: str | None = None, source: str = "upload") -> Asset:
        async def chunks():
            with open(path, "rb") as fh:
                while block := fh.read(1024 * 1024):
                    yield block

        return await self.save_stream(chunks(), filename or path.name, None, max_bytes=10**12, source=source)
