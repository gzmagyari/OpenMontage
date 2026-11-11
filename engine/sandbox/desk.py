"""A job's paid-tools desk: where a boxed agent's paid tool calls actually run.

Inside the agent box, ``BaseTool.execute`` hands every call of a tool that needs keys or the
network (runtime API or HYBRID) to this socket (``engine/sandbox/client.py``). The desk runs
in the API process, outside the box. Per call it:

1. refuses an input URL that is not on the public internet;
2. checks the job's budget and opens the ledger entry (``lib/job_ledger.py``), as the tool
   layer does for an unboxed run, in a folder the box cannot see;
3. runs the tool in a short-lived provider box: the provider keys and the network, but only
   this job's project folder and the agent box's temp folder, so an input path can reach
   nothing else;
4. records what the call cost and answers with the tool's result.

Local tools run inside the agent box itself; the desk only notes them, so the job's stage
still follows what really ran. A call that never finishes (timeout, a stopped job) keeps its
open ledger entry, which counts at its estimate: the provider may still bill it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from engine.sandbox.netcheck import AddressRefused, check_public_urls
from lib import job_ledger

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCH = Path(__file__).with_name("launch.py")
RUN_TOOL = Path(__file__).with_name("run_tool.py")
REMOTE_RUNTIMES = {"api", "hybrid"}
_MAX_REQUEST_BYTES = 32 * 1024 * 1024

_registry_lock = threading.Lock()
_registry: Any = None


def _tool_registry() -> Any:
    """The tool registry, discovered once per process (about 1.5 s)."""
    global _registry
    with _registry_lock:
        if _registry is None:
            from tools.tool_registry import registry

            registry.ensure_discovered()
            _registry = registry
    return _registry


def _failed(message: str) -> dict[str, Any]:
    return {"success": False, "data": {}, "artifacts": [], "error": message, "cost_usd": 0.0}


class ToolDesk:
    def __init__(self, *, socket_path: Path, ledger_dir: Path, project_dir: Path, shared_tmp: Path,
                 specs_dir: Path, provider_env: dict[str, str], log_path: Path,
                 timeout_seconds: float = 30 * 60, ro_paths: list[str] | None = None):
        self.socket_path = Path(socket_path)
        self.ledger_dir = Path(ledger_dir)
        self.project_dir = Path(project_dir)
        self.shared_tmp = Path(shared_tmp)
        self.specs_dir = Path(specs_dir)
        self.provider_env = dict(provider_env)
        self.log_path = Path(log_path)
        self.timeout_seconds = timeout_seconds
        self.ro_paths = ro_paths or ["/usr", "/etc", str(REPO_ROOT), "/opt"]
        self.calls = 0
        self._server: asyncio.AbstractServer | None = None
        self._running: set[asyncio.subprocess.Process] = set()
        self._local_calls: dict[str, job_ledger.ToolCall] = {}

    async def start(self) -> None:
        self.specs_dir.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.socket_path),
                                                       limit=_MAX_REQUEST_BYTES)
        os.chmod(self.socket_path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        for process in list(self._running):
            if process.returncode is None:
                process.kill()
        for process in list(self._running):
            await process.wait()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.read(_MAX_REQUEST_BYTES + 1)
            if len(raw) > _MAX_REQUEST_BYTES:
                answer = _failed("the request is too large")
            else:
                request = json.loads(raw or b"{}")
                op = request.get("op") if isinstance(request, dict) else None
                if op == "run":
                    answer = await self._run(request)
                elif op == "note":
                    answer = self._note(request)
                else:
                    answer = _failed("unknown desk request")
        except ValueError:
            answer = _failed("the desk could not read the request")
        except Exception as exc:
            log.exception("tool desk request failed")
            answer = _failed(f"the desk failed: {type(exc).__name__}")
        try:
            writer.write(json.dumps(answer, default=str).encode("utf-8"))
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    # -- local tools: noted only ------------------------------------------------------------
    def _note(self, request: dict[str, Any]) -> dict[str, Any]:
        note_id = str(request.get("id") or "")
        if request.get("event") == "start" and note_id and note_id not in self._local_calls:
            self._local_calls[note_id] = job_ledger.begin_call(
                self.ledger_dir, tool=str(request.get("tool") or "tool"), provider=request.get("provider"),
                inputs=request.get("inputs") if isinstance(request.get("inputs"), dict) else {}, estimate_usd=0.0)
        elif request.get("event") == "finish" and note_id in self._local_calls:
            job_ledger.finish_call(self.ledger_dir, self._local_calls.pop(note_id), cost_usd=0.0,
                                   success=bool(request.get("success")))
        return {"ok": True}

    # -- paid tools: run here -----------------------------------------------------------------
    async def _run(self, request: dict[str, Any]) -> dict[str, Any]:
        from tools.base_tool import _budget_quote

        name = str(request.get("tool") or "")
        inputs = request.get("inputs") if isinstance(request.get("inputs"), dict) else {}
        registry = await asyncio.to_thread(_tool_registry)
        tool = registry.get(name)
        if tool is None:
            return _failed(f"there is no tool named {name!r}")
        if getattr(tool.runtime, "value", tool.runtime) not in REMOTE_RUNTIMES:
            return _failed(f"{name} runs inside the job, not at the desk")
        try:
            await check_public_urls(inputs)
        except AddressRefused as exc:
            return _failed(f"{name} was not run: an input URL is not allowed ({exc}). Only public "
                           "internet addresses can be used.")
        cwd = str(request.get("cwd") or "")
        estimate_usd, unpriced = _budget_quote(tool, self._as_seen_from_here(inputs, cwd))
        call = job_ledger.begin_call(self.ledger_dir, tool=name, provider=getattr(tool, "provider", None),
                                     inputs=inputs, estimate_usd=estimate_usd, unpriced=unpriced)
        if call.denied:
            return _failed(call.denied)
        self.calls += 1
        result = await self._in_provider_box(tool, inputs, cwd)
        if result is None:
            return _failed(f"{name} did not finish (timed out or stopped). If the provider finished the "
                           "work it is still counted; do not repeat the call blindly.")
        # The provider's request ids go into the ledger with the cost: settlement checks each
        # estimate against what the provider really billed, and it can only do that by id.
        data = result.get("data")
        job_ledger.finish_call(self.ledger_dir, call, cost_usd=result.get("cost_usd"),
                               success=result.get("success"),
                               request_ids=data.get("request_ids") if isinstance(data, dict) else None,
                               note=data.get("operator_note") if isinstance(data, dict) else None)
        return result

    def _as_seen_from_here(self, inputs: dict[str, Any], cwd: str) -> dict[str, Any]:
        """The call's inputs with each local path spelled so that THIS process finds the file.

        A call's price can hang on a file it is given: a take is priced by the length of its
        audio. The tool itself runs in a provider box whose working folder is the job's and
        whose /tmp is the agent box's temp folder, but the price is quoted here, in the API
        process, where a path relative to the job's folder points into the repository and
        one under /tmp at this container's own. `kit voice` lists a take's audio relative to
        the project; a batch passed it as listed, and four takes of video 716 were refused
        with "no price for this call" while the files sat where the job had said.

        Only the quote reads this copy. The tool runs with the inputs as given."""
        base = Path(self._cwd(cwd))

        def here(value: Any) -> Any:
            if not isinstance(value, str) or not value or "://" in value:
                return value
            path = Path(value)
            if not path.is_absolute():
                path = base / path
            # The project is the same folder for every process; only /tmp is the box's own.
            if path.is_relative_to(self.project_dir) or not path.is_relative_to("/tmp"):
                return str(path)
            return str(self.shared_tmp / path.relative_to("/tmp"))

        seen = dict(inputs)
        for key, value in inputs.items():
            if key == "path" or key.endswith("_path"):
                seen[key] = here(value)
            elif (key == "paths" or key.endswith("_paths")) and isinstance(value, list):
                seen[key] = [here(item) for item in value]
        return seen

    def _cwd(self, requested: str) -> str:
        """The agent's working folder, if the provider box has it; else the repository."""
        for base in (REPO_ROOT, self.project_dir, Path("/tmp")):
            try:
                if requested and Path(requested).resolve().is_relative_to(base.resolve()):
                    return requested
            except (OSError, ValueError):
                continue
        return str(REPO_ROOT)

    async def _in_provider_box(self, tool: Any, inputs: dict[str, Any], cwd: str) -> dict[str, Any] | None:
        call_id = uuid.uuid4().hex
        spec_path = self.specs_dir / f"{call_id}.json"
        spec = {
            "ro": self.ro_paths,
            "rw": [[str(self.project_dir), str(self.project_dir)], [str(self.shared_tmp), "/tmp"]],
            "share_net": True,
            "env_from_parent": True,
            "chdir": self._cwd(cwd),
        }
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        payload = json.dumps({"module": type(tool).__module__, "class": type(tool).__name__, "inputs": inputs},
                             default=str).encode("utf-8")
        argv = [sys.executable, str(LAUNCH), str(spec_path), sys.executable, str(RUN_TOOL)]
        try:
            with open(self.log_path, "ab") as errors:
                errors.write(f"--- {getattr(tool, 'name', '?')} {call_id}\n".encode())
                errors.flush()
                process = await asyncio.create_subprocess_exec(
                    *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=errors,
                    env=self.provider_env, cwd=str(REPO_ROOT))
                self._running.add(process)
                try:
                    out, _ = await asyncio.wait_for(process.communicate(payload), timeout=self.timeout_seconds)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    return None
                finally:
                    self._running.discard(process)
        finally:
            spec_path.unlink(missing_ok=True)
        lines = out.decode("utf-8", "replace").strip().splitlines()
        try:
            result = json.loads(lines[-1]) if lines else None
        except ValueError:
            result = None
        if not isinstance(result, dict):
            return None if process.returncode and process.returncode < 0 else _failed(
                f"{getattr(tool, 'name', 'the tool')} ended without a result (exit {process.returncode})")
        return result
