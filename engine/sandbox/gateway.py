"""A job's Claude gateway: the boxed CLI's ANTHROPIC_BASE_URL leads here, not to the API.

The box holds a token valid for its own job, never the API key. A request passes only with
that token, only for the Messages API, only for a model the job may use, and only while the
job's metered Claude spend is under its cap; it then goes on to the Anthropic API with the
real key. The meter reads each response's usage, so the cap also covers requests that did
not come from the CLI's own loop.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

import httpx

from engine.agent.budget import BudgetMeter
from engine.sandbox.httpio import HttpError, read_body, read_head, respond_error, status_line

log = logging.getLogger(__name__)

UPSTREAM = "https://api.anthropic.com"
_MAX_BODY = 64 * 1024 * 1024
_SKIP_REQUEST = {"connection", "keep-alive", "proxy-authorization", "proxy-connection", "te", "trailer",
                 "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding",
                 "x-api-key", "authorization"}
_SKIP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding",
                  "proxy-authenticate", "trailer", "upgrade"}


class UsageReader:
    """Collects one response's token usage, from a JSON body or a Messages event stream."""

    def __init__(self, streaming: bool):
        self.streaming = streaming
        self._buffer = b""
        self.message_id: str | None = None
        self.usage: dict[str, Any] | None = None

    def feed(self, chunk: bytes) -> None:
        if not self.streaming:
            if len(self._buffer) < 8 * 1024 * 1024:
                self._buffer += chunk
            return
        self._buffer += chunk
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            if line.startswith(b"data:"):
                self._event(line[5:].strip())

    def _event(self, data: bytes) -> None:
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        if event.get("type") == "message_start":
            message = event.get("message") or {}
            self.message_id = message.get("id")
            self.usage = dict(message.get("usage") or {})
        elif event.get("type") == "message_delta" and isinstance(event.get("usage"), dict):
            # Anthropic reports a message's output tokens only here, and some Anthropic-format
            # gateways (LiteLLM) report every count here with zeros in message_start; a meter
            # reading message_start alone prices such a response at $0. Later positive counts
            # win; zeros never clobber a known count.
            merged = dict(self.usage or {})
            for key, value in event["usage"].items():
                if isinstance(value, (int, float)) and value > 0:
                    merged[key] = value
            self.usage = merged

    def close(self) -> None:
        if self.streaming or not self._buffer:
            return
        try:
            body = json.loads(self._buffer)
        except ValueError:
            return
        if isinstance(body, dict) and isinstance(body.get("usage"), dict):
            self.message_id = body.get("id")
            self.usage = dict(body["usage"])


class ClaudeGateway:
    def __init__(self, *, socket_path: Path, token: str, api_key: str, models: set[str],
                 model_prefixes: tuple[str, ...], cap_usd: float, upstream: str = UPSTREAM,
                 transport: httpx.AsyncBaseTransport | None = None,
                 on_refusal: Callable[[str], None] | None = None):
        self.socket_path = Path(socket_path)
        self._token = token
        self._api_key = api_key
        self._models = set(models)
        self._prefixes = tuple(model_prefixes)
        self.cap_usd = float(cap_usd)
        self._upstream = upstream.rstrip("/")
        self._transport = transport
        self._on_refusal = on_refusal
        self.meter = BudgetMeter(lambda: 0.0)
        self.requests = 0
        self._server: asyncio.AbstractServer | None = None
        self._client: httpx.AsyncClient | None = None
        self._slots = asyncio.Semaphore(8)

    @property
    def spend_usd(self) -> float:
        return self.meter.llm_spend()

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(connect=30.0, read=600.0, write=120.0, pool=60.0),
        )
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path))
        os.chmod(self.socket_path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._client is not None:
            await self._client.aclose()

    def _model_allowed(self, model: Any) -> bool:
        return isinstance(model, str) and (model in self._models or model.startswith(self._prefixes))

    def _refuse(self, reason: str) -> None:
        log.warning("Claude gateway refused a request: %s", reason)
        if self._on_refusal:
            self._on_refusal(reason)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await read_head(reader)
            if head is None:
                return
            body = await read_body(reader, head, _MAX_BODY)
            presented = head.header("x-api-key") or (head.header("authorization") or "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(presented.encode(), self._token.encode()):
                self._refuse("wrong token")
                await respond_error(writer, 401, "authentication_error", "invalid token for this job")
                return
            path = head.target.split("?", 1)[0]
            if head.method not in ("GET", "POST") or not path.startswith("/v1/"):
                self._refuse(f"{head.method} {path}")
                await respond_error(writer, 404, "not_found_error", "only the Anthropic API is served here")
                return
            if body:
                try:
                    payload = json.loads(body)
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and "model" in payload and not self._model_allowed(payload["model"]):
                    self._refuse(f"model {payload.get('model')!r}")
                    await respond_error(writer, 403, "permission_error", "this model is not available to this job")
                    return
            if self.spend_usd >= self.cap_usd:
                self._refuse(f"cap reached at ${self.spend_usd:.2f}")
                await respond_error(writer, 403, "permission_error",
                                    "this job's Claude budget is used up; finish with what exists")
                return
            async with self._slots:
                await self._forward(head, body, writer)
        except HttpError as exc:
            await respond_error(writer, exc.status, "invalid_request_error", exc.message)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        except Exception:  # never let one request take the gateway down
            log.exception("Claude gateway request failed")
            try:
                await respond_error(writer, 502, "api_error", "the gateway could not reach the Anthropic API")
            except Exception:
                pass
        finally:
            writer.close()

    async def _forward(self, head: Any, body: bytes, writer: asyncio.StreamWriter) -> None:
        assert self._client is not None
        headers = [(k, v) for k, v in head.headers if k.lower() not in _SKIP_REQUEST]
        headers += [("x-api-key", self._api_key), ("accept-encoding", "identity")]
        self.requests += 1
        request = self._client.build_request(head.method, self._upstream + head.target, headers=headers,
                                             content=body or None)
        response = await self._client.send(request, stream=True)
        try:
            writer.write(status_line(response.status_code, response.reason_phrase))
            for key, value in response.headers.multi_items():
                if key.lower() not in _SKIP_RESPONSE:
                    writer.write(f"{key}: {value}\r\n".encode("latin-1", "replace"))
            writer.write(b"connection: close\r\n\r\n")
            usage = UsageReader("text/event-stream" in response.headers.get("content-type", ""))
            # Decoded bytes: content-encoding is not passed on (identity was asked for anyway).
            async for chunk in response.aiter_bytes():
                usage.feed(chunk)
                writer.write(chunk)
                await writer.drain()
            usage.close()
            if usage.usage:
                self.meter.add_usage(usage.usage, usage.message_id)
        finally:
            await response.aclose()
