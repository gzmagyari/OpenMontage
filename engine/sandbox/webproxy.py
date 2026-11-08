"""A job's web proxy: the box's HTTP(S)_PROXY. Public websites only.

It serves WebFetch and downloads (a product page the customer linked, its photos). Every
connection is to a public internet address on port 80 or 443, checked after the name is
resolved and made to the checked address, so a name that points inside (this machine, the
LAN, the container network, cloud metadata) is refused. HTTPS goes through CONNECT and
stays end-to-end encrypted; the proxy sees only the host name. Each job has a byte budget
and a connection limit, and every connection is logged to the job's web.log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

from engine.sandbox.httpio import HttpError, read_body, read_head, respond
from engine.sandbox.netcheck import AddressRefused, resolve_public

log = logging.getLogger(__name__)

_SKIP_HEADERS = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}


class WebProxy:
    def __init__(self, *, socket_path: Path, log_path: Path, ports: tuple[int, ...] = (80, 443),
                 max_bytes: int = 2 * 1024 ** 3, max_connections: int = 16, idle_seconds: float = 120.0):
        self.socket_path = Path(socket_path)
        self.log_path = Path(log_path)
        self.ports = ports
        self.max_bytes = max_bytes
        self.idle_seconds = idle_seconds
        self.bytes_used = 0
        self._slots = asyncio.Semaphore(max_connections)
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def _log(self, entry: dict) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}) + "\n")
        except OSError:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.monotonic()
        entry: dict = {}
        try:
            async with self._slots:
                head = await read_head(reader)
                if head is None:
                    return
                if head.method == "CONNECT":
                    host, _, port_text = head.target.rpartition(":")
                    port = int(port_text) if port_text.isdigit() else 443
                    path = None
                else:
                    parts = urlsplit(head.target)
                    if parts.scheme.lower() != "http" or not parts.hostname:
                        raise HttpError(400, "send https through CONNECT and http as an absolute URL")
                    host, port = parts.hostname, parts.port or 80
                    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                host = host.strip("[]")
                entry = {"host": host, "port": port, "method": head.method}
                if port not in self.ports:
                    raise HttpError(403, f"port {port} is not allowed")
                if self.bytes_used >= self.max_bytes:
                    raise HttpError(403, "this job's download allowance is used up")
                try:
                    infos = await resolve_public(host, port)
                except AddressRefused as exc:
                    raise HttpError(403, str(exc)) from exc
                address = infos[0][4][0]
                upstream_reader, upstream_writer = await asyncio.wait_for(
                    asyncio.open_connection(address, port), timeout=15.0)
                try:
                    if path is None:
                        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        await writer.drain()
                    else:
                        body = await read_body(reader, head, 64 * 1024 * 1024)
                        lines = [f"{head.method} {path} HTTP/1.1"]
                        lines += [f"{k}: {v}" for k, v in head.headers if k.lower() not in _SKIP_HEADERS]
                        lines += ["connection: close", "", ""]
                        upstream_writer.write("\r\n".join(lines).encode("latin-1", "replace") + body)
                        await upstream_writer.drain()
                    up, down = await asyncio.gather(self._pipe(reader, upstream_writer),
                                                    self._pipe(upstream_reader, writer))
                    entry.update(bytes_up=up, bytes_down=down)
                finally:
                    upstream_writer.close()
        except HttpError as exc:
            entry.update(refused=exc.message)
            try:
                await respond(writer, exc.status, exc.message.encode("utf-8"), content_type="text/plain")
            except Exception:
                pass
        except (asyncio.TimeoutError, OSError) as exc:
            entry.update(error=type(exc).__name__)
            try:
                await respond(writer, 502, b"could not connect", content_type="text/plain")
            except Exception:
                pass
        except Exception:
            log.exception("web proxy connection failed")
        finally:
            writer.close()
            if entry:
                entry["seconds"] = round(time.monotonic() - started, 2)
                self._log(entry)

    async def _pipe(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
        moved = 0
        try:
            while True:
                chunk = await asyncio.wait_for(reader.read(64 * 1024), timeout=self.idle_seconds)
                if not chunk:
                    break
                moved += len(chunk)
                self.bytes_used += len(chunk)
                if self.bytes_used > self.max_bytes:
                    break
                writer.write(chunk)
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, OSError):
            pass
        finally:
            try:
                if writer.can_write_eof():
                    writer.write_eof()
            except (OSError, RuntimeError):
                pass
        return moved
