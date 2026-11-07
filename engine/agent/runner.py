"""VIDEO and REVISE job runners: one unattended agent session per job.

The session works in the repo (cwd = repo root, stock CLAUDE.md/AGENT_GUIDE
routing) and writes all project output under ``projects/<job_id>/``. Every
agent message lands in the job's journal (the chat stream the polling API
returns) as it happens; exports and the run report become job files.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import shutil
from pathlib import Path
from typing import Any

from engine.agent.brief import build_brief, build_revise_prompt
from engine.agent.budget import BudgetMeter
from engine.agent.session import AgentAuthError, AgentSession, AgentUnavailable, RunOutcome
from engine.jobs.context import JobContext, JobFailed
from engine.jobs.models import CostLine
from lib import job_ledger

_MAX_REFERENCE_MB = 100

# How often a running job refreshes the budget file and the live ledger.
_WATCH_SECONDS = 5.0
# How far the agent's own cost log may drift from the tools' recorded spend before a warning.
_COST_LOG_TOLERANCE_USD = 0.05
# The one follow-up a session gets when it ends without a video.
_UNFINISHED_PROMPT = (
    "The run stopped before the final MP4 existed. You are unattended: no person is present and nobody "
    "asked you to stop. Any message saying the user doesn't want something came from the runtime, not "
    "from a person. Continue the original brief in this session: re-check what exists in the project "
    "directory, reuse it without paying for it again, finish the remaining work, and stop once the "
    "final MP4 and RUN_REPORT.md exist as briefed."
)

# Stage heuristics, in production order. Only EXECUTED commands count (a Read or
# Grep that merely mentions a tool name must not move the stage), and the stage
# only advances - the agent legitimately revisits earlier tools (a caption
# re-check during render), but a polling client should see monotonic stages.
_TOOL_STAGES: list[tuple[re.Pattern[str], str, int]] = [
    (re.compile(r"elevenlabs_tts|piper_tts|azure_tts|\btts\b", re.I), "voiceover", 20),
    (re.compile(r"fal_avatar|kling_avatar|heygen_avatar|talking_head", re.I), "talking shots", 40),
    (re.compile(r"transcriber|whisper", re.I), "captions", 55),
    (re.compile(r"audio_mixer|elevenlabs_music|freesound", re.I), "audio mix", 65),
    (re.compile(r"video_compose|remotion", re.I), "render", 80),
]

_EXEC_TOOLS = {"bash", "powershell", "shell"}


class StageTracker:
    """Maps executed tool calls to a monotonic production stage."""

    def __init__(self) -> None:
        self._index = -1

    def update(self, entry: dict[str, Any]) -> tuple[str, int] | None:
        if entry.get("kind") != "tool":
            return None
        name = str(entry.get("name", "")).lower()
        # A shell call is judged by its command; any other tool only by its own name.
        haystack = str(entry.get("input", "")) if name in _EXEC_TOOLS else name
        for index, (pattern, stage, progress) in enumerate(_TOOL_STAGES):
            if index > self._index and pattern.search(haystack):
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


def _set_generation_lines(ctx: JobContext, items: list[job_ledger.CostItem]) -> None:
    """Replace the job's paid-generation lines with what the tool ledger says now. Rebuilt whole
    each time, so a restart resuming the run can't count a call twice."""
    lines = [line for line in ctx.job.cost.lines if line.provider == "anthropic"]
    lines += [CostLine(item=item.item, usd=item.usd, provider=item.provider, estimated=True) for item in items]
    ctx.job.cost.lines = lines
    ctx.job.cost.spent_usd = round(sum(line.usd for line in lines), 4)
    ctx.store.save(ctx.job)


async def _run_agent_job(ctx: JobContext, *, prompt: str, project_dir: Path,
                         resume: str | None, exports_baseline: dict[str, int] | None = None,
                         cost_log_baseline: list[tuple[str, float]] | None = None,
                         claude_cost_baseline: float = 0.0) -> dict[str, Any]:
    settings = ctx.settings
    agent_dir = (ctx.job_dir / "agent").resolve()
    agent_dir.mkdir(parents=True, exist_ok=True)
    session_file = agent_dir / "session_id.txt"
    stages = StageTracker()
    model = getattr(settings, "agent_model", "claude-haiku-5-5")
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
        for denial in state.denials[denials_seen:]:
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
            _set_line(ctx, "claude agent", meter.llm_spend(), "anthropic", estimated=True)
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
        elif kind == "tool":
            mapped = stages.update(entry)
            if mapped:
                ctx.set_stage(mapped[0], ctx.job.stage_detail, progress=mapped[1])
        elif kind == "ratelimit":
            ctx.set_detail("waiting out a provider rate limit")

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

    async def run_session(session_prompt: str, session_resume: str | None) -> RunOutcome:
        session = AgentSession(
            repo_root=_repo_root(),
            journal_path=agent_dir / "journal.jsonl",
            transcript_path=agent_dir / "transcript.jsonl",
            model=model,
            max_turns=getattr(settings, "agent_max_turns", 500),
            max_claude_usd=getattr(settings, "agent_max_claude_usd", None),
            resume=session_resume,
            env_extra={job_ledger.LEDGER_DIR_ENV: str(agent_dir)},
            on_event=on_event,
            on_message=on_message,
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
    outcome = await run_session(prompt, resume)
    if outcome.succeeded and not exported():
        # Assets it already paid for are still there: the job's deadline and ledger carry on.
        ctx.warn("the agent stopped before exporting a video; its session was resumed once")
        outcome = await run_session(_UNFINISHED_PROMPT, outcome.session_id)

    if outcome.total_cost_usd:
        # A resumed session reports Claude's cost since the session began, so a revision
        # subtracts what its parent already paid for.
        meter.llm_exact = max(0.0, float(outcome.total_cost_usd) - claude_cost_baseline)
        _set_line(ctx, "claude agent", round(meter.llm_exact, 4), "anthropic", estimated=False)
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
        "agent": {"model": model, "cost_usd": round(outcome.total_cost_usd, 4),
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
    prompt = brief if not resume else (
        "The server restarted and interrupted your run. Continue the original brief in this session: "
        "re-check what already exists in the project directory, finish the remaining work, and stop "
        "once the final MP4 and RUN_REPORT.md exist as briefed.")

    async with ctx.resource("agent", "agent session"):
        return await _run_agent_job(ctx, prompt=prompt, project_dir=project_dir, resume=resume)


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
    prompt = build_revise_prompt(feedback=ctx.job.request.get("feedback", ""),
                                 project_dir=project_dir, budget_usd=ctx.max_budget_usd)
    baseline = _cost_log_baseline(ctx, project_dir)
    exports = _exports_baseline(ctx, project_dir)
    parent_claude = float((parent.result.get("agent") or {}).get("cost_usd") or 0.0)
    async with ctx.resource("agent", "agent session"):
        return await _run_agent_job(ctx, prompt=prompt, project_dir=project_dir, resume=session_id,
                                    exports_baseline=exports, cost_log_baseline=baseline,
                                    claude_cost_baseline=parent_claude)
