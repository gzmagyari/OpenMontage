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
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk import query as sdk_query  # tests monkeypatch engine.agent.session.sdk_query
from claude_agent_sdk.types import AgentDefinition, EffortLevel

# Nothing inherited may redirect auth or behaviour: the session env is rebuilt
# from scratch and exactly one credential is injected (saasbatch's SCRUB rule).
_SCRUB = re.compile(r"^(ANTHROPIC|CLAUDE|AI_AGENT|BAGGAGE|OTEL_|SENTRY_)", re.IGNORECASE)

# A job is one continuous turn, on the tools production runs use. An allow-list: wake-ups, cron
# and workflows all let the agent end its turn with work still running, and a later CLI's new
# tools stay out until someone decides otherwise. Task joined for FOREGROUND subagents only:
# several issued in one message run concurrently and the director blocks until all return, so
# work still never outlives the turn (delegation rules: AGENT_GUIDE "Delegation (Binding)").
AGENT_TOOLS = ["Bash", "Read", "Write", "Edit", "Skill", "Task", "WebFetch", "WebSearch"]

# The delegation pool: mechanical work runs on the side model, in parallel, at side-model
# rates (the gateway prices claude-haiku-* traffic separately). Creative decisions, QC
# verdicts and paid provider calls stay with the director.
MECHANIC_AGENT = AgentDefinition(
    description="Fast mechanical worker for file wrangling, ffmpeg probes, frame "
                "extraction and mechanical checks. Give it a self-contained brief with exact "
                "input and output paths.",
    prompt=(
        "You are a production mechanic inside a video job's sandbox. Do exactly the mechanical "
        "task in your brief - file operations, ffmpeg probes and frame extraction, durations, "
        "dimensions and decode checks - then report concisely what "
        "you did and what you verified, naming the files you produced. Never call paid "
        "generation tools, never browse the web, never make creative decisions; if the brief "
        "needs any of that, stop and say so instead of improvising."
    ),
    tools=["Bash", "Read", "Write", "Edit"],
    model="claude-haiku-5-5",
)

# The custom-scene author: the video kit assembles a video from the director's edit, so there
# is no composition to draft. This subagent writes only the scenes a video needs that the kit
# does not have, in parallel with the provider burst, and only when the plan lists any. The
# director reviews them and owns the final cut. Model is a deployment knob
# (OPENMONTAGE_COMPOSER_MODEL).
COMPOSER_MODEL_DEFAULT = "claude-sonnet-5-5"


def composer_agent(model: str | None = None) -> AgentDefinition:
    return AgentDefinition(
        description="Custom-scene author: writes the scenes a video needs that the video kit "
                    "does not have. Give it the absolute project path and, for each scene, its "
                    "name, what it shows and when, its props, and the look it has to belong to. "
                    "Use it only when the plan lists custom scenes.",
        prompt=(
            "You write custom scenes for one video inside its job sandbox. The video kit "
            "assembles the video from the director's edit; you write only the scenes in your "
            "brief, the ones the kit does not have. First read /app/remotion-composer/kit/"
            "README.md (\"Your own scenes\" and \"Ready-made scenes\") and, for Remotion "
            "itself, /app/.agents/skills/remotion-best-practices/rules/. Your brief names the "
            "absolute project path; work there (the repo root you start in is read-only). For "
            "each scene write composition/src/scenes/<Name>.tsx and register it in "
            "composition/src/scenes.ts. Build on the kit's helpers from '@kit/engine' and "
            "'@kit/scenes' (useShot, useWordFrame, useDress, surface, Backdrop, PushIn, "
            "HEAD_START) and on the job's look in composition/src/look, so the scene belongs "
            "to this video; it must show something on its first frame. Write creative, "
            "purpose-built code for this video's plan, never a generic template. Typecheck "
            "with `npx tsc -p tsconfig.json` in the project's composition directory and fix "
            "what fails. Do not touch composition/edit.json, the look, the kit, or any file "
            "outside the scenes you were asked for. Then report concisely: the scene names, "
            "their props, and anything you had to assume. Never call paid generation tools, "
            "never browse the web, never render the final export, never touch cost or "
            "billing files, and never override the plan's creative decisions; if the brief "
            "needs any of that, stop and say so."
        ),
        tools=["Bash", "Read", "Write", "Edit"],
        model=model or COMPOSER_MODEL_DEFAULT,
    )


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


def anthropic_api_key(repo_root: Path) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY") or _dotenv_value(repo_root, "ANTHROPIC_API_KEY")
    if not api_key:
        raise AgentUnavailable("ANTHROPIC_API_KEY is not set (environment or .env)")
    return api_key


def cli_switches() -> dict[str, str]:
    """The CLI behaviour every job needs, boxed or not."""
    return {
        # Unattended runs ride out transient 429/529s instead of failing the turn.
        "CLAUDE_CODE_RETRY_WATCHDOG": "1",
        "CLAUDE_CODE_RETRY_WATCHDOG_MAX_WAIT_MS": str(30 * 60 * 1000),
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        # A slow provider call waits in the foreground. Past the stock 120 s the CLI would move the
        # command to the background, and the agent would end its turn to wait for it.
        "BASH_DEFAULT_TIMEOUT_MS": str(20 * 60 * 1000),
        "BASH_MAX_TIMEOUT_MS": str(30 * 60 * 1000),
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    }


def session_env(repo_root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """An UNBOXED session's environment (development hosts and tests): scrubbed, keyed, venv on
    PATH. A boxed session gets its environment from engine/sandbox/box.py instead."""
    env = {key: value for key, value in os.environ.items() if not _SCRUB.match(key)}
    env["ANTHROPIC_API_KEY"] = anthropic_api_key(repo_root)
    fal_key = os.environ.get("FAL_KEY") or _dotenv_value(repo_root, "FAL_KEY")
    if fal_key:
        env["FAL_KEY"] = fal_key
    # Repo tools (piper, whisper) resolve binaries via PATH from the venv.
    venv_scripts = repo_root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    if venv_scripts.is_dir():
        env["PATH"] = str(venv_scripts) + os.pathsep + env.get("PATH", "")
    env.update(cli_switches())
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
        effort: EffortLevel | None = None,
        max_claude_usd: float | None = None,
        composer_model: str | None = None,
        resume: str | None = None,
        fork: bool = False,
        env_extra: dict[str, str] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        on_message: Callable[[Any], None] | None = None,
        on_tool: Callable[[str, dict[str, Any]], None] | None = None,
        cli_path: str | None = None,
        box_env: dict[str, str] | None = None,
        settings_json: str | None = None,
        activity: Any = None,
    ) -> None:
        self.repo_root = repo_root
        # The job's live view (engine/agent/activity.py). With it, the session asks for partial
        # messages: a step then shows from the moment the model starts writing a tool call, not
        # only once the call is written and issued.
        self.activity = activity
        # A boxed session (engine/sandbox/box.py): the SDK starts the box's launcher, whose
        # environment is only the job's token; the box's own environment is in its spec.
        self.cli_path = cli_path
        self.box_env = box_env
        self.settings_json = settings_json
        self.journal_path = journal_path
        self.transcript_path = transcript_path
        self.model = model
        self.max_turns = max_turns
        self.effort = effort
        self.max_claude_usd = max_claude_usd
        self.composer_model = composer_model
        self.resume = resume
        self.fork = fork              # resume as a new session, leaving the original untouched
        self.env_extra = env_extra
        self.on_event = on_event
        self.on_message = on_message  # every raw SDK message, before journaling (live accounting)
        self.on_tool = on_tool        # every tool call, with its whole input (the journal clips it)

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
            # A helper's own calls (a subagent's) belong to the step of the call that started it.
            own = not getattr(message, "parent_tool_use_id", None)
            for block in message.content or []:
                if isinstance(block, TextBlock) and block.text and block.text.strip():
                    self._journal({"kind": "assistant", "text": block.text})
                    outcome.final_text = block.text
                elif isinstance(block, ToolUseBlock):
                    self._journal({"kind": "tool", "name": block.name, "id": block.id,
                                   "input": _clip(block.input, 1500)})
                    if self.activity is not None and own:
                        self.activity.tool_called(block.id, block.name, block.input)
                    if self.on_tool:
                        self.on_tool(block.name, block.input if isinstance(block.input, dict) else {})
                elif isinstance(block, ThinkingBlock):
                    pass  # not part of the public chat stream
        elif isinstance(message, UserMessage):
            content = message.content if isinstance(message.content, list) else []
            for block in content:
                if isinstance(block, ToolResultBlock):
                    self._journal({"kind": "tool_result", "tool_use_id": block.tool_use_id,
                                   "is_error": bool(block.is_error),
                                   "text": _clip(_tool_result_text(block.content), 2000)})
                    if self.activity is not None:
                        self.activity.tool_finished(block.tool_use_id)
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
        env = dict(self.box_env) if self.box_env is not None else session_env(self.repo_root, self.env_extra)
        options = ClaudeAgentOptions(
            cli_path=self.cli_path,
            settings=self.settings_json,
            cwd=str(self.repo_root),
            model=self.model,
            effort=self.effort,
            max_turns=self.max_turns,
            max_budget_usd=self.max_claude_usd,
            permission_mode="bypassPermissions",
            env=env,
            # Load the repo's CLAUDE.md -> AGENT_GUIDE.md exactly like an interactive run.
            setting_sources=["project"],
            tools=AGENT_TOOLS,
            agents={"mechanic": MECHANIC_AGENT, "composer": composer_agent(self.composer_model)},
            system_prompt={"type": "preset", "preset": "claude_code"},
            resume=self.resume,
            fork_session=self.fork,
            # A tool result carrying a large file (an image read, a long log) overflows the
            # default 1 MiB transport buffer and kills the session; 32 MiB absorbs real runs.
            max_buffer_size=32 * 1024 * 1024,
            include_partial_messages=self.activity is not None,
        )
        outcome = RunOutcome(session_id=self.resume)
        stream = sdk_query(prompt=prompt, options=options)
        try:
            async for message in stream:
                if isinstance(message, StreamEvent):
                    # The model's answer as it is being written: only the live view reads it. It is
                    # never journaled, metered or kept (a job has thousands of these).
                    if self.activity is not None:
                        self.activity.stream_event(message.event, message.parent_tool_use_id)
                    continue
                self._transcript(message)
                if self.on_message:
                    self.on_message(message)
                self._record(message, outcome)
        finally:
            if self.activity is not None:
                self.activity.settle()
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # the transport is already gone
                    pass
        return outcome
