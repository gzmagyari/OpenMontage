"""The little HTTP/1.1 the box sockets speak: one request per connection, then close."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

MAX_HEAD_BYTES = 64 * 1024


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class RequestHead:
    method: str
    target: str
    version: str
    headers: list[tuple[str, str]] = field(default_factory=list)

    def header(self, name: str) -> str | None:
        name = name.lower()
        for key, value in self.headers:
            if key.lower() == name:
                return value
        return None


async def read_head(reader: asyncio.StreamReader) -> RequestHead | None:
    """The request line and headers, or None when the client closed without sending one."""
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as exc:
        if not exc.partial.strip():
            return None
        raise HttpError(400, "incomplete request") from exc
    except asyncio.LimitOverrunError as exc:
        raise HttpError(431, "request headers too large") from exc
    lines = raw.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise HttpError(400, "malformed request line")
    head = RequestHead(method=parts[0].upper(), target=parts[1], version=parts[2])
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep or not name.strip():
            raise HttpError(400, "malformed header")
        head.headers.append((name.strip(), value.strip()))
    return head


async def read_body(reader: asyncio.StreamReader, head: RequestHead, limit: int) -> bytes:
    """The request body by Content-Length or chunked encoding, refused past ``limit`` bytes."""
    if "chunked" in (head.header("transfer-encoding") or "").lower():
        body = bytearray()
        while True:
            size_line = await reader.readuntil(b"\r\n")
            size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                await reader.readuntil(b"\r\n")  # the (empty) trailer section
                return bytes(body)
            if len(body) + size > limit:
                raise HttpError(413, "request body too large")
            body += await reader.readexactly(size)
            await reader.readexactly(2)
    length = int(head.header("content-length") or 0)
    if length > limit:
        raise HttpError(413, "request body too large")
    return await reader.readexactly(length) if length else b""


def status_line(status: int, reason: str = "") -> bytes:
    return f"HTTP/1.1 {status} {reason or 'Status'}\r\n".encode("latin-1")


async def respond(writer: asyncio.StreamWriter, status: int, body: bytes,
                  content_type: str = "application/json", reason: str = "") -> None:
    writer.write(status_line(status, reason))
    writer.write(f"content-type: {content_type}\r\ncontent-length: {len(body)}\r\nconnection: close\r\n\r\n"
                 .encode("latin-1"))
    writer.write(body)
    await writer.drain()


async def respond_error(writer: asyncio.StreamWriter, status: int, kind: str, message: str) -> None:
    """An error in the Anthropic API's own shape, which the CLI reports as is."""
    payload = {"type": "error", "error": {"type": kind, "message": message}}
    await respond(writer, status, json.dumps(payload).encode("utf-8"))
