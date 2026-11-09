"""VIDEO and REVISE job runners: one unattended agent session per job.

The session works in the repo (cwd = repo root, stock CLAUDE.md/AGENT_GUIDE
routing) and writes all project output under ``projects/<job_id>/``. Every
agent message lands in the job's journal (the chat stream the polling API
returns) as it happens; exports and the run report become job files.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import os
import re
import shutil
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.agent.brief import build_brief, build_revise_prompt
from engine.agent.budget import BudgetMeter
from engine.agent.session import (AgentAuthError, AgentSession, AgentUnavailable, RunOutcome,
                                  anthropic_api_key, cli_switches)
from engine.jobs.context import JobContext, JobFailed
from engine.jobs.models import CostLine
from lib import job_ledger

if TYPE_CHECKING:  # imported where it is used: engine.sandbox imports engine.agent
    from engine.sandbox.box import AgentBox

_MAX_REFERENCE_MB = 100

# How often a running job refreshes the budget file and the live ledger.
_WATCH_SECONDS = 5.0
# How far the agent's own cost log may drift from the tools' recorded spend before a warning.
_COST_LOG_TOLERANCE_USD = 0.05
# What a session is told when the server comes back in the middle of its run.
_RESTARTED_PROMPT = (
    "The server restarted and interrupted your run. Continue the original brief in this session: "
    "re-check what already exists in the project directory, finish the remaining work, and stop "
    "once the final MP4 and RUN_REPORT.md exist as briefed."
)
# What a stopped job's session is told when the client resumes it.
_RESUME_PROMPT = (
    "The client stopped this run before it finished and now wants it finished. You are unattended "
    "again: never pause for approval. Continue the original brief in this session: re-check what "
    "already exists in the project directory, reuse it without paying for it again, finish the "
    "remaining work, and stop once the final MP4 and RUN_REPORT.md exist as briefed."
)
# The one follow-up a session gets when it ends without a video.
_UNFINISHED_PROMPT = (
    "The run stopped before the final MP4 existed. You are unattended: no person is present and nobody "
    "asked you to stop. Any message saying the user doesn't want something came from the runtime, not "
    "from a person. Continue the original brief in this session: re-check what exists in the project "
    "directory, reuse it without paying for it again, finish the remaining work, and stop once the "
    "final MP4 and RUN_REPORT.md exist as briefed."
)
# The one follow-up a session gets when the job gateway's model-call cap ended it mid-run.
_WRAP_PROMPT = (
    "The model-call budget for this production is used up, so this is a short wrap-up session. "
    "Do not generate any new assets and do not call any paid tool. Assemble the best possible video "
    "from the assets that already exist in the project, render it with Remotion, export the final MP4 "
    "under exports/, and write RUN_REPORT.md noting the early wrap-up. If an export already exists, "
    "verify it and stop."
)
_LLM_BUDGET_MARKER = "Claude budget is used up"


def _llm_budget_death(exc: BaseException) -> bool:
    """The boxed CLI exits hard (a 403 it reads as an auth failure) when the job gateway refuses
    further model calls at its cap; recognize that exit so the run can wrap up instead of failing."""
    return type(exc).__name__ == "ResultError" and _LLM_BUDGET_MARKER in str(exc)

# Stages in production order, by the tool that marks each one. The job's tool ledger
# says what really ran, so an agent reading a tool's source or a schema that mentions
# it can't move the stage. The stage only advances - the agent legitimately revisits
# earlier tools (a caption re-check during render), but a polling client should see
# monotonic stages.
_TOOL_STAGES: list[tuple[re.Pattern[str], str, int]] = [
    (re.compile(r"tts", re.I), "voiceover", 20),
    (re.compile(r"avatar|talking_head", re.I), "talking shots", 40),
    (re.compile(r"transcriber|whisper", re.I), "captions", 55),
    (re.compile(r"audio_mixer|music|freesound", re.I), "audio mix", 65),
    (re.compile(r"video_compose|remotion|hyperframes", re.I), "render", 80),
]
# The one production step the agent may run outside the tools: Remotion straight from the shell.
_DIRECT_RENDER = re.compile(r"(?:^|&&|;)\s*(?:timeout\s+\d+\s+)?npx\s+remotion\s+render\b")


class StageTracker:
    """Maps the tools that really ran to a monotonic production stage."""

    def __init__(self) -> None:
        self._index = -1
        self.tools_seen = 0

    def update(self, tool: str) -> tuple[str, int] | None:
        for index, (pattern, stage, progress) in enumerate(_TOOL_STAGES):
            if index > self._index and pattern.search(tool):
                self._index = index
                return stage, progress
        return None


def _file_entry(path: Path) -> dict[str, Any]:
    return {"name": path.name, "size_bytes": path.stat().st_size,
            "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}


def _repo_root() -> Path:
    from lib.paths import REPO_ROOT

    return Path(REPO_ROOT)


# -- references --------------------------------------------------------------------
async def _download_reference(url: str, dest: Path) -> None:
    import httpx

    limit = _MAX_REFERENCE_MB * 1024 * 1024
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                size = 0
                with open(dest, "wb") as fh:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        size += len(chunk)
                        if size > limit:
                            raise JobFailed("reference_too_large",
                                            f"Reference {url!r} exceeds {_MAX_REFERENCE_MB} MB")
                        fh.write(chunk)
    except JobFailed:
        raise
    except Exception as exc:
        raise JobFailed("reference_download_failed", f"Could not download reference {url!r}: {exc}") from exc


async def _resolve_references(ctx: JobContext, refs_dir: Path) -> list[dict[str, Any]]:
    from engine.assets import safe_filename

    resolved: list[dict[str, Any]] = []
    for index, ref in enumerate(ctx.job.request.get("references") or [], start=1):
        role = ref.get("role") or "reference"
        if ref.get("asset_id"):
            asset = ctx.services.assets.get(ref["asset_id"]) if ctx.services else None
            if asset is None:
                raise JobFailed("unknown_asset", f"Unknown asset {ref['asset_id']!r}")
            source = ctx.services.assets.path(ref["asset_id"])
            dest = refs_dir / safe_filename(f"{index:02d}_{role}_{asset.filename}", f"ref_{index}")
            await asyncio.to_thread(shutil.copy2, source, dest)
        else:
            url = ref["url"]
            name = Path(url.split("?", 1)[0]).name or f"ref_{index}"
            dest = refs_dir / safe_filename(f"{index:02d}_{role}_{name}", f"ref_{index}")
            await _download_reference(url, dest)
        resolved.append({"path": str(dest), "role": role, "label": ref.get("label")})
    return resolved


# -- the shared agent-run core ---------------------------------------------------------
def _read_cost_log(ctx: JobContext, project_dir: Path) -> list[tuple[str, float]]:
    """Best-effort read of the agent's own cost log: (item, usd) entries."""
    entries_out: list[tuple[str, float]] = []
    for candidate in (project_dir / "artifacts" / "cost_log.json", project_dir / "cost_log.json"):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            ctx.warn(f"could not parse {candidate.name}")
            continue
        entries = data if isinstance(data, list) else data.get("entries") or data.get("calls") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            usd = next((entry[k] for k in ("usd", "cost", "cost_usd", "actual") if isinstance(entry.get(k), (int, float))), None)
            if usd is None or usd <= 0:
                continue
            item = str(entry.get("item") or entry.get("tool") or entry.get("model") or "generation")[:200]
            entries_out.append((item, float(usd)))
        break
    return entries_out


def _beyond_baseline(entries: list[tuple[str, float]], baseline: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """The cost-log entries a run added on top of `baseline` (compared as a multiset, so an agent
    that rewrites the file in a different order still matches)."""
    from collections import Counter

    remaining = Counter(baseline)
    added = []
    for entry in entries:
        if remaining[entry] > 0:
            remaining[entry] -= 1
        else:
            added.append(entry)
    return added


def _cost_log_baseline(ctx: JobContext, project_dir: Path) -> list[tuple[str, float]]:
    """What the project's cost log already held when this job first started.

    A revision works in the FIRST job's project and keeps logging to the same file, so without
    this baseline its ledger would re-count everything the earlier runs bought. Persisted per
    job, so a revision resumed after a restart keeps the entries it logged before the restart."""
    path = ctx.job_dir / "agent" / "cost_log_baseline.json"
    if path.is_file():
        try:
            return [(str(item), float(usd)) for item, usd in json.loads(path.read_text(encoding="utf-8"))]
        except (OSError, ValueError, TypeError):
            ctx.warn("could not read the cost-log baseline; counting the whole project log")
            return []
    baseline = _read_cost_log(ctx, project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline), encoding="utf-8")
    return baseline


def _exports_baseline(ctx: JobContext, project_dir: Path) -> dict[str, int]:
    """The MP4s the project already held when this job first started (name to mtime).

    A revision works in the FIRST job's project, so the earlier exports are still in the folder.
    Persisted per job, so a revision resumed after a restart still counts what it exported itself."""
    path = ctx.job_dir / "agent" / "exports_baseline.json"
    if path.is_file():
        try:
            return {str(name): int(mtime) for name, mtime in json.loads(path.read_text(encoding="utf-8")).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            ctx.warn("could not read the exports baseline; counting only new file names")
    baseline = {p.name: p.stat().st_mtime_ns for p in (project_dir / "exports").glob("*.mp4") if p.is_file()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline), encoding="utf-8")
    return baseline


def _set_line(ctx: JobContext, item: str, usd: float, provider: str, estimated: bool) -> None:
    """Create or update one cost line in place, keeping spent_usd consistent."""
    if usd <= 0:
        return
    for line in ctx.job.cost.lines:
        if line.item == item and line.provider == provider:
            delta = round(usd - line.usd, 4)
            line.usd = round(usd, 4)
            line.estimated = estimated
            ctx.job.cost.spent_usd = round(ctx.job.cost.spent_usd + delta, 4)
            ctx.store.save(ctx.job)
            return
    ctx.add_cost(item, usd, provider=provider, estimated=estimated)


def _stop_leftovers(agent_dir: Path) -> int:
    """Stop what a finished session left running. The agent can detach a command with nohup, and
    then a render keeps its CPUs and a provider call keeps spending after the job has ended. All of
    a session's processes carry its ledger folder in their environment. Needs /proc (Linux)."""
    marker = f"{job_ledger.LEDGER_DIR_ENV}={agent_dir}".encode()
    stopped = 0
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if marker in (proc / "environ").read_bytes().split(b"\0"):
                os.kill(int(proc.name), signal.SIGTERM)
                stopped += 1
        except (OSError, ValueError):
            continue
    return stopped


def _set_generation_lines(ctx: JobContext, items: list[job_ledger.CostItem]) -> None:
    """Replace the job's paid-generation lines with what the tool ledger says now. Rebuilt whole
    each time, so a restart resuming the run can't count a call twice."""
    lines = [line for line in ctx.job.cost.lines if line.provider == "anthropic"]
    lines += [CostLine(item=item.item, usd=item.usd, provider=item.provider, estimated=True) for item in items]
    ctx.job.cost.lines = lines
    ctx.job.cost.spent_usd = round(sum(line.usd for line in lines), 4)
    ctx.store.save(ctx.job)


async def _run_agent_job(ctx: JobContext, *, prompt: str, project_dir: Path,
                         resume: str | None, fork: bool = False, exports_baseline: dict[str, int] | None = None,
                         cost_log_baseline: list[tuple[str, float]] | None = None,
                         claude_cost_baseline: float = 0.0) -> dict[str, Any]:
    settings = ctx.settings
    agent_dir = (ctx.job_dir / "agent").resolve()
    agent_dir.mkdir(parents=True, exist_ok=True)
    session_file = agent_dir / "session_id.txt"
    # A resume of this job (POST /v1/jobs/{id}/resume) continues here, whatever kind of job it was.
    (agent_dir / "project_dir.txt").write_text(str(project_dir), encoding="utf-8")
    stages = StageTracker()
    model = getattr(settings, "agent_model", "claude-haiku-5-5")
    effort = getattr(settings, "agent_effort", None)
    wall_minutes = getattr(settings, "agent_wall_time_minutes", 45)

    # Live money state. Every tool call in the session checks the budget file and appends to the
    # job's tool ledger; the runner keeps the file current and mirrors the ledger into the job.
    meter = BudgetMeter(lambda: ctx.max_budget_usd)
    written_budget: float | None = None
    ledger_items: list[job_ledger.CostItem] = []
    denials_seen = 0

    def sync_budget() -> None:
        # POST /v1/jobs/{id}/budget changes the job's options; the tools read this file.
        nonlocal written_budget
        budget = ctx.max_budget_usd
        if budget != written_budget:
            job_ledger.write_budget(agent_dir, budget)
            written_budget = budget

    def sync_ledger() -> job_ledger.LedgerState:
        nonlocal ledger_items, denials_seen
        state = job_ledger.summarize(job_ledger.read_entries(agent_dir))
        meter.actuals_total = state.spent_usd
        if state.items != ledger_items:
            ledger_items = state.items
            _set_generation_lines(ctx, state.items)
        for tool in state.tools_started[stages.tools_seen:]:
            mapped = stages.update(tool)
            if mapped:
                ctx.set_stage(mapped[0], ctx.job.stage_detail, progress=mapped[1])
        stages.tools_seen = len(state.tools_started)
        for denial in state.denials[denials_seen:]:
            if denial.get("reason") == "unpriced":
                ctx.warn(f"refused a {denial.get('tool')} call: the tool can't quote a price, "
                         "so it can't run in a paid job")
                continue
            ctx.set_detail("paid-generation budget reached; finishing with what exists")
            ctx.warn(f"budget enforcement denied a {denial.get('tool')} call at "
                     f"${float(denial.get('spent_usd') or 0):.2f} spent of the "
                     f"${float(denial.get('budget_usd') or 0):.2f} budget")
        denials_seen = len(state.denials)
        return state

    def on_message(message: Any) -> None:
        from claude_agent_sdk import AssistantMessage, UserMessage
        if isinstance(message, AssistantMessage):
            meter.add_usage(getattr(message, "usage", None), getattr(message, "message_id", None))
            _set_line(ctx, "claude agent", meter.llm_spend(), llm_provider, estimated=True)
        elif isinstance(message, UserMessage):
            sync_ledger()

    def on_event(entry: dict[str, Any]) -> None:
        kind = entry.get("kind")
        if kind == "session" and entry.get("session_id"):
            session_file.write_text(str(entry["session_id"]), encoding="utf-8")
        elif kind == "assistant":
            text = str(entry.get("text", "")).strip().splitlines()[0][:160]
            if text:
                ctx.set_detail(text)
        elif kind == "ratelimit":
            ctx.set_detail("waiting out a provider rate limit")

    def on_tool(name: str, tool_input: dict[str, Any]) -> None:
        if name == "Bash" and _DIRECT_RENDER.search(str(tool_input.get("command") or "")):
            mapped = stages.update("remotion")
            if mapped:
                ctx.set_stage(mapped[0], ctx.job.stage_detail, progress=mapped[1])

    async def watch() -> None:
        """Keeps the money state current during a long tool call; returns if spend passes the ceiling."""
        while True:
            await asyncio.sleep(_WATCH_SECONDS)
            try:
                sync_budget()
            except OSError as exc:
                ctx.warn(f"could not refresh the budget file: {exc}")
            sync_ledger()
            if meter.generation_spend() > meter.budget() * 1.5 + 0.50:
                return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + wall_minutes * 60
    # The CLI keeps a session's history under its config folder, by default in the container's
    # home. Here it lives in the project on the data volume: a revision resumes it, a restart
    # resumes it, a redeploy can't lose it, and it expires with the project.
    session_home = (project_dir / ".agent_session").resolve()
    session_home.mkdir(parents=True, exist_ok=True)
    # Each session runs in its own box (engine/sandbox/): no provider key, no other job and no
    # network inside; Claude, paid tools and public websites only through the job's sockets.
    boxed = getattr(settings, "agent_sandbox", "off") == "required"
    claude_cap = float(getattr(settings, "agent_max_claude_usd", None) or 3.0)
    llm_upstream = getattr(settings, "agent_llm_upstream", None)
    if llm_upstream and not boxed:
        raise JobFailed("agent_unavailable",
                        "A custom LLM upstream needs the boxed agent runtime; set "
                        "OPENMONTAGE_AGENT_SANDBOX=required or unset the upstream.")
    llm_provider = "llm-gateway" if llm_upstream else "anthropic"
    gateway_spend = 0.0

    async def run_session(session_prompt: str, session_resume: str | None, session_fork: bool = False,
                          claude_cap_override: float | None = None) -> RunOutcome:
        from engine.sandbox.box import AgentBox, SandboxUnavailable  # engine.sandbox imports engine.agent

        async with contextlib.AsyncExitStack() as stack:
            box: AgentBox | None = None
            if boxed:
                # The SDK's version probe would start a box of its own; the CLI version is pinned.
                os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
                try:
                    switches = cli_switches()
                    if llm_upstream:
                        # The CLI's side calls use a small Claude model; pin the name so the
                        # upstream gateway can route it explicitly.
                        switches.setdefault("ANTHROPIC_SMALL_FAST_MODEL", "claude-haiku-5-5")
                        switches.setdefault("ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-5-5")
                    box = await stack.enter_async_context(AgentBox(
                        agent_dir=agent_dir, project_dir=project_dir, model=model,
                        # The SDK caps the CLI's own loop; the gateway backstops anything else.
                        # Behind a custom upstream the CLI cannot price the model, so the
                        # gateway cap IS the enforcement and gets no slack.
                        claude_cap_usd=claude_cap_override or claude_cap * (1.0 if llm_upstream else 1.5),
                        cli_switches=switches, llm_upstream=llm_upstream,
                        api_key=(getattr(settings, "agent_llm_upstream_key", None)
                                 if llm_upstream else anthropic_api_key(_repo_root())),
                        warn=ctx.warn))
                except SandboxUnavailable as exc:
                    raise JobFailed("agent_unavailable", f"The agent's box could not start: {exc}")
                except AgentUnavailable as exc:
                    raise JobFailed("agent_unavailable", str(exc))
            return await run_boxed_session(session_prompt, session_resume, session_fork, box)

    async def run_boxed_session(session_prompt: str, session_resume: str | None, session_fork: bool,
                                box: AgentBox | None) -> RunOutcome:
        nonlocal gateway_spend
        session = AgentSession(
            repo_root=_repo_root(),
            journal_path=agent_dir / "journal.jsonl",
            transcript_path=agent_dir / "transcript.jsonl",
            model=model,
            effort=effort,
            max_turns=getattr(settings, "agent_max_turns", 500),
            max_claude_usd=getattr(settings, "agent_max_claude_usd", None),
            resume=session_resume,
            fork=session_fork,
            env_extra=None if box else {job_ledger.LEDGER_DIR_ENV: str(agent_dir), "CLAUDE_CONFIG_DIR": str(session_home)},
            on_event=on_event,
            on_message=on_message,
            on_tool=on_tool,
            cli_path=box.cli_path if box else None,
            box_env=box.session_env() if box else None,
            settings_json=box.settings_json if box else None,
        )
        run_task = asyncio.create_task(session.run(session_prompt))
        watch_task = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait({run_task, watch_task}, return_when=asyncio.FIRST_COMPLETED,
                                         timeout=max(0.0, deadline - loop.time()))
            if run_task in done:
                return run_task.result()
            if watch_task in done:
                # The tool layer denies paid calls past the budget, so crossing this
                # means the enforcement was bypassed (say, parallel calls racing it).
                raise JobFailed("budget_exhausted",
                                f"Paid generation reached ${meter.generation_spend():.2f}, past 1.5x the "
                                f"${meter.budget():.2f} budget; the run was stopped.",
                                retryable=False)
            raise JobFailed("agent_timeout",
                            f"The agent run exceeded {wall_minutes} minutes "
                            "and was stopped; the session can be resumed.", retryable=True)
        except AgentUnavailable as exc:
            raise JobFailed("agent_unavailable", str(exc))
        except AgentAuthError as exc:
            raise JobFailed("agent_auth", f"Auth self-check failed: {exc}")
        finally:
            # Whatever ended this, a cancelled job included: the agent stops spending with it,
            # and the job keeps a record of everything spent so far.
            run_task.cancel()
            watch_task.cancel()
            await asyncio.gather(run_task, watch_task, return_exceptions=True)
            if box is not None:
                # The box's processes end with it; the gateway's meter covers all its Claude traffic.
                gateway_spend += box.gateway.spend_usd if box.gateway else 0.0
            elif stopped := _stop_leftovers(agent_dir):
                ctx.log(f"stopped {stopped} processes the session left running")
            sync_ledger()

    def exported() -> list[Path]:
        exports_dir = project_dir / "exports"
        videos = sorted((p for p in exports_dir.glob("*.mp4") if p.is_file()),
                        key=lambda p: p.stat().st_mtime) if exports_dir.is_dir() else []
        if exports_baseline is not None:
            videos = [p for p in videos if exports_baseline.get(p.name) != p.stat().st_mtime_ns]
        return videos

    sync_budget()
    ctx.set_stage("agent", "the agent is reading the brief", progress=10)
    try:
        outcome = await run_session(prompt, resume, fork)
    except Exception as exc:
        if not _llm_budget_death(exc):
            raise
        # The gateway refused further model calls at its cap and the CLI died on the 403. The
        # paid assets are on disk: resume the same session once, on a small wrap-up allowance,
        # to assemble and export what exists instead of failing the job.
        wrap_resume = session_file.read_text(encoding="utf-8").strip() if session_file.exists() else None
        if not wrap_resume:
            raise
        ctx.warn("the model-call budget ran out mid-production; wrapping up with what exists")
        outcome = await run_session(_WRAP_PROMPT, wrap_resume,
                                    claude_cap_override=max(1.0, claude_cap * 0.25))
    if outcome.succeeded and not exported():
        # Assets it already paid for are still there: the job's deadline and ledger carry on.
        ctx.warn("the agent stopped before exporting a video; its session was resumed once")
        outcome = await run_session(_UNFINISHED_PROMPT, outcome.session_id)

    if outcome.total_cost_usd:
        # A resumed session reports Claude's cost since the session began, so a revision
        # subtracts what its parent had paid for when it finished.
        meter.llm_exact = max(0.0, float(outcome.total_cost_usd) - claude_cost_baseline)
        _set_line(ctx, "claude agent", round(meter.llm_exact, 4), llm_provider, estimated=False)
    if boxed and gateway_spend > (meter.llm_exact or 0.0) * 1.25 + 0.10:
        # Claude traffic outside the agent loop's own accounting reached the gateway: bill what
        # the gateway metered rather than the loop's lower figure.
        ctx.warn(f"the Claude gateway metered ${gateway_spend:.2f} against the agent loop's "
                 f"${meter.llm_exact or 0.0:.2f}; billing the metered figure")
        _set_line(ctx, "claude agent", round(gateway_spend, 4), llm_provider, estimated=True)
    # What the tools recorded is the bill. The agent's own cost log is a cross-check, and the
    # fallback if the ledger stayed empty, so a broken ledger can't bill $0. A revision counts
    # only what it added to the shared project log.
    log_entries = _read_cost_log(ctx, project_dir)
    if cost_log_baseline:
        log_entries = _beyond_baseline(log_entries, cost_log_baseline)
    log_total = round(sum(usd for _, usd in log_entries), 4)
    ledger = sync_ledger()
    if not ledger.recorded:
        if log_entries:
            ctx.warn("the tool ledger recorded nothing; billing the agent's own cost log")
            for item, usd in log_entries:
                ctx.add_cost(item, usd, provider="fal.ai", estimated=True)
    elif log_entries and abs(log_total - ledger.spent_usd) > _COST_LOG_TOLERANCE_USD:
        ctx.warn(f"the agent's cost log (${log_total:.2f}) differs from the tools' recorded spend "
                 f"(${ledger.spent_usd:.2f}); billing the recorded spend")

    if not outcome.succeeded:
        detail = outcome.result_text or outcome.final_text or outcome.subtype or "unknown agent error"
        raise JobFailed("agent_failed", f"The agent run ended without success "
                                        f"({outcome.subtype}): {str(detail)[:1500]}", retryable=True)

    videos = exported()
    if not videos:
        raise JobFailed("no_video", "The agent finished without an MP4 under exports/ "
                                    f"(looked in {project_dir / 'exports'})", retryable=True)
    ctx.set_stage("collecting", "collecting exports", progress=92)

    files: dict[str, dict[str, Any]] = {}
    for path in videos:
        await asyncio.to_thread(shutil.copy2, path, ctx.files_dir / path.name)
        files[path.stem] = _file_entry(ctx.files_dir / path.name)
    main_video = files[videos[-1].stem]
    for extra in (project_dir / "RUN_REPORT.md", agent_dir / "journal.jsonl", agent_dir / "transcript.jsonl"):
        if extra.is_file():
            await asyncio.to_thread(shutil.copy2, extra, ctx.files_dir / extra.name)
            files[extra.stem] = _file_entry(ctx.files_dir / extra.name)

    return {
        "video": main_video,
        "files": files,
        "session_id": outcome.session_id,
        "project_dir": str(project_dir),
        "agent": {"model": model, "effort": effort, "llm_upstream": llm_upstream,
                  "cost_usd": round(outcome.total_cost_usd, 4),
                  "turns": outcome.num_turns, "duration_ms": outcome.duration_ms},
    }


# -- the registered runners ---------------------------------------------------------
async def run_video(ctx: JobContext) -> dict[str, Any]:
    options = ctx.job.request.get("options") or {}
    project_dir = ctx.project_dir
    refs_dir = project_dir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)

    ctx.set_stage("preparing", "resolving references", progress=3)
    references = await _resolve_references(ctx, refs_dir)

    brief = build_brief(
        prompt=ctx.job.request.get("prompt", ""),
        references=references,
        project_dir=project_dir,
        budget_usd=ctx.max_budget_usd,
        duration_seconds=float(options.get("duration_seconds") or 30),
        language=str(options.get("language") or "en"),
        captions=bool(options.get("captions", True)),
        music=bool(options.get("music", True)),
    )
    agent_dir = ctx.job_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "brief.md").write_text(brief, encoding="utf-8")

    # A restart mid-run resumes the same session instead of re-briefing (and re-spending).
    session_file = agent_dir / "session_id.txt"
    resume = session_file.read_text(encoding="utf-8").strip() if session_file.is_file() else None
    prompt = brief if not resume else _RESTARTED_PROMPT

    async with ctx.resource("agent", "agent session"):
        return await _run_agent_job(ctx, prompt=prompt, project_dir=project_dir, resume=resume)


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _session_claude_total(store: Any, job: Any) -> float:
    """What the CLI has reported for the session up to the end of ``job``. A resumed or forked
    session's total includes everything before it, so a run built on ``job`` subtracts this. A
    finished job reported it; a stopped one did not, so its own Claude line is added to what the
    session had cost before it."""
    agent = (job.result or {}).get("agent") or {}
    if isinstance(agent.get("cost_usd"), (int, float)):
        return float(agent["cost_usd"])
    own = sum(line.usd for line in job.cost.lines if line.provider == "anthropic")
    parent = store.get(job.parent_job_id) if job.parent_job_id and job.kind != "video" else None
    return own + (_session_claude_total(store, parent) if parent is not None else 0.0)


async def run_resume(ctx: JobContext) -> dict[str, Any]:
    """Continue a stopped job: the same project, the same agent session (forked, like a revision),
    reusing everything it already made. Billed only for what this run adds."""
    store = ctx.services.store if (ctx.services and ctx.services.store) else None
    parent_id = ctx.job.request.get("parent_job_id")
    parent = store.get(parent_id) if store else None
    if parent is None or not parent.stopped:
        raise JobFailed("parent_not_resumable", f"Job {parent_id!r} is gone or was not stopped")
    parent_agent = ctx.store.job_dir(str(parent_id)) / "agent"
    project_dir = Path(_read_text(parent_agent / "project_dir.txt") or (ctx.settings.projects_dir / str(parent_id)))
    if not project_dir.is_dir():
        raise JobFailed("parent_expired", "The stopped job's project directory is gone; submit a new video job instead")
    own_session = _read_text(ctx.job_dir / "agent" / "session_id.txt")
    parent_session = _read_text(parent_agent / "session_id.txt")
    claude_baseline = 0.0
    if own_session:  # the server restarted in the middle of this resume
        prompt, resume, fork = _RESTARTED_PROMPT, own_session, False
        claude_baseline = _session_claude_total(store, parent) if parent_session else 0.0
    elif parent_session:
        prompt, resume, fork = _RESUME_PROMPT, parent_session, True
        claude_baseline = _session_claude_total(store, parent)
    else:  # stopped before its session began: start the session it never had
        prompt = _read_text(parent_agent / "brief.md")
        if not prompt:
            raise JobFailed("parent_not_resumable", "The stopped job never started its agent session; submit it again")
        resume, fork = None, False
    baseline = _cost_log_baseline(ctx, project_dir)
    async with ctx.resource("agent", "agent session"):
        # No exports baseline: a video the stopped run already exported is this video, and the
        # newest export is the one returned.
        return await _run_agent_job(ctx, prompt=prompt, project_dir=project_dir, resume=resume, fork=fork,
                                    cost_log_baseline=baseline, claude_cost_baseline=claude_baseline)


async def run_revise(ctx: JobContext) -> dict[str, Any]:
    parent_id = ctx.job.request.get("parent_job_id")
    parent = ctx.services.store.get(parent_id) if (ctx.services and ctx.services.store) else None
    if parent is None or not parent.result:
        raise JobFailed("parent_not_found", f"Parent job {parent_id!r} is gone or has no result")
    session_id = parent.result.get("session_id")
    project_dir = Path(parent.result.get("project_dir") or (ctx.settings.projects_dir / str(parent_id)))
    if not project_dir.is_dir():
        raise JobFailed("parent_expired", "The parent job's project directory was cleaned up; "
                                          "submit a new video job instead")
    # A revision forks the parent's session, so an earlier revision that failed is neither billed
    # to this one nor in its history. A restart mid-run picks up its own fork.
    agent_dir = ctx.job_dir / "agent"
    session_file = agent_dir / "session_id.txt"
    own_session = session_file.read_text(encoding="utf-8").strip() if session_file.is_file() else None
    prompt = _RESTARTED_PROMPT if own_session else build_revise_prompt(
        feedback=ctx.job.request.get("feedback", ""), project_dir=project_dir, budget_usd=ctx.max_budget_usd)
    baseline = _cost_log_baseline(ctx, project_dir)
    exports = _exports_baseline(ctx, project_dir)
    parent_claude = float((parent.result.get("agent") or {}).get("cost_usd") or 0.0)
    async with ctx.resource("agent", "agent session"):
        return await _run_agent_job(ctx, prompt=prompt, project_dir=project_dir, resume=own_session or session_id,
                                    fork=not own_session, exports_baseline=exports, cost_log_baseline=baseline,
                                    claude_cost_baseline=parent_claude)
