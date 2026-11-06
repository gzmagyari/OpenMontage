"""One unattended agent session on the Claude Agent SDK.

A Python port of the battle-tested session layer in the user's saasbatch
project (server/src/sdk.mjs): scrubbed per-session environment, auth
self-check against the init message, a per-message journal (the job's chat
stream), raw transcript persistence, and cost taken from the result message.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk import query as sdk_query  # tests monkeypatch engine.agent.session.sdk_query

# Nothing inherited may redirect auth or behaviour: the session env is rebuilt
# from scratch and exactly one credential is injected (saasbatch's SCRUB rule).
_SCRUB = re.compile(r"^(ANTHROPIC|CLAUDE|AI_AGENT|BAGGAGE|OTEL_|SENTRY_)", re.IGNORECASE)


class AgentUnavailable(Exception):
    """The agent cannot run in this deployment (missing credential or SDK)."""


class AgentAuthError(Exception):
    """The session authenticated with a different credential than the one provided."""


def _dotenv_value(repo_root: Path, name: str) -> str | None:
    try:
        for line in (repo_root / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                return value or None
    except OSError:
        pass
    return None


def session_env(repo_root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """A clean environment for one session: scrubbed, keyed, venv on PATH."""
    env = {key: value for key, value in os.environ.items() if not _SCRUB.match(key)}
    api_key = os.environ.get("ANTHROPIC_API_KEY") or _dotenv_value(repo_root, "ANTHROPIC_API_KEY")
    if not api_key:
        raise AgentUnavailable("ANTHROPIC_API_KEY is not set (environment or .env)")
    env["ANTHROPIC_API_KEY"] = api_key
    fal_key = os.environ.get("FAL_KEY") or _dotenv_value(repo_root, "FAL_KEY")
    if fal_key:
        env["FAL_KEY"] = fal_key
    # Repo tools (piper, whisper) resolve binaries via PATH from the venv.
    venv_scripts = repo_root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    if venv_scripts.is_dir():
        env["PATH"] = str(venv_scripts) + os.pathsep + env.get("PATH", "")
    # Unattended runs ride out transient 429/529s instead of failing the turn.
    env["CLAUDE_CODE_RETRY_WATCHDOG"] = "1"
    env["CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS"] = str(30 * 60 * 1000)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    if extra:
        env.update(extra)
    return env


def _clip(value: Any, max_len: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if text and len(text) > max_len:
        return text[:max_len] + f"... [+{len(text) - max_len} chars]"
    return text


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif part.get("type") == "image":
                parts.append("[image]")
    return "\n".join(parts)


@dataclasses.dataclass
class RunOutcome:
    session_id: str | None = None
    subtype: str | None = None
    is_error: bool = True
    total_cost_usd: float = 0.0
    num_turns: int = 0
    duration_ms: int = 0
    final_text: str = ""
    result_text: str = ""

    @property
    def succeeded(self) -> bool:
        return self.subtype == "success" and not self.is_error


class AgentSession:
    """Runs one prompt to completion, journaling every message as it arrives."""

    def __init__(
        self,
        *,
        repo_root: Path,
        journal_path: Path,
        transcript_path: Path,
        model: str,
        max_turns: int,
        max_claude_usd: float | None = None,
        resume: str | None = None,
        env_extra: dict[str, str] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_message: Callable[[Any], None] | None = None,
        hooks: dict[str, Any] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.journal_path = journal_path
        self.transcript_path = transcript_path
        self.model = model
        self.max_turns = max_turns
        self.max_claude_usd = max_claude_usd
        self.resume = resume
        self.env_extra = env_extra
        self.on_event = on_event
        self.on_message = on_message  # every raw SDK message, before journaling (live accounting)
        self.hooks = hooks            # SDK hook config (the budget's PreToolUse enforcement)

    # -- persistence ---------------------------------------------------------------
    def _write_line(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def _journal(self, entry: dict[str, Any]) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}
        self._write_line(self.journal_path, entry)
        if self.on_event:
            self.on_event(entry)

    def _transcript(self, message: Any) -> None:
        try:
            raw = dataclasses.asdict(message)
        except TypeError:
            raw = {"repr": repr(message)}
        self._write_line(self.transcript_path, {"type": type(message).__name__, **raw})

    # -- message recording (the chat stream) ----------------------------------------
    def _record(self, message: Any, outcome: RunOutcome) -> None:
        if isinstance(message, SystemMessage):
            if message.subtype == "init":
                data = message.data or {}
                outcome.session_id = data.get("session_id") or outcome.session_id
                self._journal({"kind": "session", "session_id": outcome.session_id,
                               "model": data.get("model"), "tools": len(data.get("tools") or [])})
                # The auth self-check: the credential the runtime actually used.
                source = data.get("apiKeySource")
                if source and source != "ANTHROPIC_API_KEY":
                    raise AgentAuthError(
                        f"the session authenticated via {source!r}, not the provided ANTHROPIC_API_KEY")
            elif message.subtype == "api_retry":
                data = message.data or {}
                self._journal({"kind": "system", "text": f"API retry {data.get('attempt')}/"
                                                         f"{data.get('max_retries')} ({data.get('error_status')})"})
        elif isinstance(message, AssistantMessage):
            for block in message.content or []:
                if isinstance(block, TextBlock) and block.text and block.text.strip():
                    self._journal({"kind": "assistant", "text": block.text})
                    outcome.final_text = block.text
                elif isinstance(block, ToolUseBlock):
                    self._journal({"kind": "tool", "name": block.name, "id": block.id,
                                   "input": _clip(block.input, 1500)})
                elif isinstance(block, ThinkingBlock):
                    pass  # not part of the public chat stream
        elif isinstance(message, UserMessage):
            content = message.content if isinstance(message.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    self._journal({"kind": "tool_result", "tool_use_id": block.tool_use_id,
                                   "is_error": bool(block.is_error),
                                   "text": _clip(_tool_result_text(block.content), 2000)})
        elif isinstance(message, RateLimitEvent):
            info = message.rate_limit_info
            try:
                info = dataclasses.asdict(info)
            except TypeError:
                info = {"info": str(info)}
            self._journal({"kind": "ratelimit", **{k: v for k, v in info.items() if v is not None}})
        elif isinstance(message, ResultMessage):
            outcome.subtype = message.subtype
            outcome.is_error = bool(message.is_error)
            outcome.total_cost_usd = float(message.total_cost_usd or 0.0)
            outcome.num_turns = int(message.num_turns or 0)
            outcome.duration_ms = int(message.duration_ms or 0)
            outcome.session_id = message.session_id or outcome.session_id
            outcome.result_text = str(message.result or "")
            self._journal({"kind": "result", "subtype": message.subtype, "is_error": outcome.is_error,
                           "turns": outcome.num_turns, "duration_ms": outcome.duration_ms,
                           "cost_usd": outcome.total_cost_usd})

    # -- run -------------------------------------------------------------------------
    async def run(self, prompt: str) -> RunOutcome:
        env = session_env(self.repo_root, self.env_extra)
        options = ClaudeAgentOptions(
            cwd=str(self.repo_root),
            model=self.model,
            max_turns=self.max_turns,
            max_budget_usd=self.max_claude_usd,
            permission_mode="bypassPermissions",
            env=env,
            # Load the repo's CLAUDE.md -> AGENT_GUIDE.md exactly like an interactive run.
            setting_sources=["project"],
            system_prompt={"type": "preset", "preset": "claude_code"},
            resume=self.resume,
            hooks=self.hooks or None,
            # A tool result carrying a large file (an image read, a long log) overflows the
            # default 1 MiB transport buffer and kills the session; 32 MiB absorbs real runs.
            max_buffer_size=32 * 1024 * 1024,
        )
        outcome = RunOutcome(session_id=self.resume)
        stream = sdk_query(prompt=prompt, options=options)
        try:
            async for message in stream:
                self._transcript(message)
                if self.on_message:
                    self.on_message(message)
                self._record(message, outcome)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # the transport is already gone
                    pass
        return outcome
