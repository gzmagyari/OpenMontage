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
from engine.agent.budget import BudgetMeter, classify, make_hooks, parse_costs
from engine.agent.session import AgentAuthError, AgentSession, AgentUnavailable, _tool_result_text
from engine.jobs.context import JobContext, JobFailed

_MAX_REFERENCE_MB = 100

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


def _remove_lines(ctx: JobContext, lines: list) -> None:
    for line in lines:
        if line in ctx.job.cost.lines:
            ctx.job.cost.lines.remove(line)
            ctx.job.cost.spent_usd = round(ctx.job.cost.spent_usd - line.usd, 4)
    ctx.store.save(ctx.job)


async def _run_agent_job(ctx: JobContext, *, prompt: str, project_dir: Path,
                         resume: str | None, started_after: float | None = None,
                         cost_log_baseline: list[tuple[str, float]] | None = None) -> dict[str, Any]:
    settings = ctx.settings
    agent_dir = ctx.job_dir / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    session_file = agent_dir / "session_id.txt"
    stages = StageTracker()

    # Live money state: the budget is read from the job's options on every
    # decision, so POST /v1/jobs/{id}/budget clamps a running job.
    meter = BudgetMeter(lambda: ctx.max_budget_usd)
    live_lines: list = []           # generation lines added live, replaced by the cost log at the end
    seen_results: set[str] = set()  # tool_use_ids whose results were already parsed
    call_kinds: dict[str, str] = {}

    def on_deny(kind: str, floor: float) -> None:
        ctx.set_detail("paid-generation budget reached; finishing with what exists")
        ctx.warn(f"budget enforcement denied a {kind} call (floor ${floor:.2f}) at "
                 f"${meter.generation_spend():.2f} spent of the ${meter.budget():.2f} budget")

    def on_message(message: Any) -> None:
        from claude_agent_sdk import AssistantMessage, ToolResultBlock, ToolUseBlock, UserMessage
        if isinstance(message, AssistantMessage):
            meter.add_usage(getattr(message, "usage", None))
            for block in message.content or []:
                if isinstance(block, ToolUseBlock):
                    paid = classify(block.name, block.input if isinstance(block.input, dict) else {})
                    if paid:
                        call_kinds[block.id] = paid[0]
            _set_line(ctx, "claude agent", meter.llm_spend(), "anthropic", estimated=True)
        elif isinstance(message, UserMessage):
            content = message.content if isinstance(message.content, list) else []
            for block in content:
                if not isinstance(block, ToolResultBlock) or block.tool_use_id in seen_results:
                    continue
                seen_results.add(block.tool_use_id)
                kind = call_kinds.get(block.tool_use_id, "generation")
                for usd in parse_costs(_tool_result_text(block.content)):
                    meter.add_actual(usd)
                    ctx.add_cost(f"{kind} call (reported live)", usd, provider="fal.ai", estimated=True)
                    live_lines.append(ctx.job.cost.lines[-1])

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

    session = AgentSession(
        repo_root=_repo_root(),
        journal_path=agent_dir / "journal.jsonl",
        transcript_path=agent_dir / "transcript.jsonl",
        model=getattr(settings, "agent_model", "claude-haiku-5-5"),
        max_turns=getattr(settings, "agent_max_turns", 500),
        max_claude_usd=getattr(settings, "agent_max_claude_usd", None),
        resume=resume,
        on_event=on_event,
        on_message=on_message,
        hooks=make_hooks(meter, on_deny=on_deny),
    )
    ctx.set_stage("agent", "the agent is reading the brief", progress=10)
    # The PreToolUse hook denies paid calls past the budget, so this ceiling is a
    # backstop for classifier gaps; crossing it means the enforcement failed.
    run_task = asyncio.create_task(session.run(prompt))

    async def _ceiling() -> None:
        while True:
            await asyncio.sleep(5)
            if meter.generation_spend() > meter.budget() * 1.5 + 0.50:
                return

    ceiling_task = asyncio.create_task(_ceiling())
    try:
        done, _ = await asyncio.wait({run_task, ceiling_task}, return_when=asyncio.FIRST_COMPLETED,
                                     timeout=getattr(settings, "agent_wall_time_minutes", 45) * 60)
        if run_task in done:
            outcome = run_task.result()
        else:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
            if ceiling_task in done:
                raise JobFailed("budget_exhausted",
                                f"Paid generation reached ${meter.generation_spend():.2f}, past 1.5x the "
                                f"${meter.budget():.2f} budget; the run was stopped. The PreToolUse "
                                "enforcement should make this unreachable; check the paid-call classifier.",
                                retryable=False)
            raise JobFailed("agent_timeout",
                            f"The agent run exceeded {getattr(settings, 'agent_wall_time_minutes', 45)} minutes "
                            "and was stopped; the session can be resumed.", retryable=True)
    except AgentUnavailable as exc:
        raise JobFailed("agent_unavailable", str(exc))
    except AgentAuthError as exc:
        raise JobFailed("agent_auth", f"Auth self-check failed: {exc}")
    finally:
        ceiling_task.cancel()
        await asyncio.gather(ceiling_task, return_exceptions=True)

    if outcome.total_cost_usd:
        meter.llm_exact = float(outcome.total_cost_usd)
        _set_line(ctx, "claude agent", round(outcome.total_cost_usd, 4), "anthropic", estimated=False)
    # The agent's own cost log is the richer record (item names, superseded calls);
    # it replaces the live lines unless it reports LESS than the observed spend. A revision
    # counts only what it added to the shared project log.
    log_entries = _read_cost_log(ctx, project_dir)
    if cost_log_baseline:
        log_entries = _beyond_baseline(log_entries, cost_log_baseline)
    log_total = round(sum(usd for _, usd in log_entries), 4)
    if log_total >= meter.actuals_total - 0.01:
        _remove_lines(ctx, live_lines)
        for item, usd in log_entries:
            ctx.add_cost(item, usd, provider="fal.ai", estimated=True)
    elif live_lines:
        ctx.warn(f"the agent's cost log (${log_total:.2f}) reports less than the observed tool "
                 f"spend (${meter.actuals_total:.2f}); keeping the observed lines")

    if not outcome.succeeded:
        detail = outcome.result_text or outcome.final_text or outcome.subtype or "unknown agent error"
        raise JobFailed("agent_failed", f"The agent run ended without success "
                                        f"({outcome.subtype}): {str(detail)[:1500]}", retryable=True)

    ctx.set_stage("collecting", "collecting exports", progress=92)
    exports_dir = project_dir / "exports"
    videos = sorted((p for p in exports_dir.glob("*.mp4") if p.is_file()),
                    key=lambda p: p.stat().st_mtime) if exports_dir.is_dir() else []
    if started_after is not None:
        fresh = [p for p in videos if p.stat().st_mtime >= started_after]
        videos = fresh or videos
    if not videos:
        raise JobFailed("no_video", "The agent finished without an MP4 under exports/ "
                                    f"(looked in {exports_dir})", retryable=True)

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
        "agent": {"model": session.model, "cost_usd": round(outcome.total_cost_usd, 4),
                  "turns": outcome.num_turns, "duration_ms": outcome.duration_ms},
    }


# -- the registered runners ---------------------------------------------------------
async def run_video(ctx: JobContext) -> dict[str, Any]:
    import time

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

    started = time.time()
    async with ctx.resource("agent", "agent session"):
        return await _run_agent_job(ctx, prompt=prompt, project_dir=project_dir,
                                    resume=resume, started_after=None if resume else started)


async def run_revise(ctx: JobContext) -> dict[str, Any]:
    import time

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
    started = time.time()
    async with ctx.resource("agent", "agent session"):
        return await _run_agent_job(ctx, prompt=prompt, project_dir=project_dir,
                                    resume=session_id, started_after=started, cost_log_baseline=baseline)
