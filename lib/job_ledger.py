"""One job's paid-tool ledger and budget, shared across the processes of an agent run.

The agent reaches paid tools however it likes: a ``python -c`` over the registry, a
script it wrote a minute ago. No check on its commands can see them all, so the
budget lives where every paid call has to pass: ``BaseTool.execute``
(tools/base_tool.py). The job's runner points the session at a folder through
OPENMONTAGE_JOB_LEDGER_DIR, and every tool call in that session, in whatever process,
reads the budget there and appends what it spent. Outside a job the variable is unset
and nothing here runs.

Two files:

- ``budget.json``: ``{"max_budget_usd": n}``, rewritten by the runner when the job's
  budget changes.
- ``tool_ledger.jsonl``: one line per paid call's start, finish or denial. Appends are
  single lines, so parallel processes don't tear each other's entries, and a malformed
  line is skipped on read.

A job's spend is every finished call's reported cost, plus the estimate of a call that
started and hasn't finished: it may still be billed (a killed command, a provider
still rendering). A later success for the same output replaces such a call, so a resume
or a retry after a timeout counts once. A successful call that reports no cost counts at
its quote. A tool with no price for a call (PriceQuoteRequired) is refused, since its
spend could be neither billed nor held to the budget.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LEDGER_DIR_ENV = "OPENMONTAGE_JOB_LEDGER_DIR"
BUDGET_FILENAME = "budget.json"
LEDGER_FILENAME = "tool_ledger.jsonl"


def active_dir() -> Path | None:
    """The running job's ledger folder, or None outside a job."""
    value = os.environ.get(LEDGER_DIR_ENV)
    return Path(value) if value else None


# -- budget ------------------------------------------------------------------------------
def write_budget(directory: Path, max_budget_usd: float) -> None:
    """Atomic, so a tool in another process never reads half a file."""
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / f"{BUDGET_FILENAME}.{os.getpid()}.tmp"
    temp.write_text(json.dumps({"max_budget_usd": max_budget_usd}), encoding="utf-8")
    os.replace(temp, directory / BUDGET_FILENAME)


def read_budget(directory: Path) -> float | None:
    try:
        return float(json.loads((directory / BUDGET_FILENAME).read_text(encoding="utf-8"))["max_budget_usd"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def deny_message(spent_usd: float, budget_usd: float, tool: str, estimate_usd: float | None) -> str:
    needs = f"needs about ${estimate_usd:.2f} more" if estimate_usd else "cannot be quoted"
    return (
        f"PAID-GENERATION BUDGET REACHED: ${spent_usd:.2f} of the ${budget_usd:.2f} budget is spent "
        f"and this {tool} call {needs}. Buy nothing further: no retries of this call, no other paid "
        "tools. Finish the video NOW from the assets that already exist - assemble, render and export "
        "the best cut, then write RUN_REPORT.md noting what the budget could not cover."
    )


def unpriced_message(tool: str) -> str:
    return (
        f"NO PRICE FOR THIS CALL: {tool} cannot quote what this call would cost, so it cannot run in "
        "a paid job (its spend could be neither billed nor held to the budget). Nothing was spent and "
        "the budget is unchanged. Do this step with a tool or model that quotes its price, or without it."
    )


# -- the ledger --------------------------------------------------------------------------
@dataclass(frozen=True)
class CostItem:
    item: str
    usd: float
    provider: str | None = None
    # Provider-side request ids (fal), so settlement can replace the estimate with what the
    # provider's billing events really charged; () = not reconcilable, stays an estimate.
    request_ids: tuple[str, ...] = ()
    estimated: bool = True


@dataclass
class LedgerState:
    items: list[CostItem] = field(default_factory=list)
    denials: list[dict[str, Any]] = field(default_factory=list)
    tools_started: list[str] = field(default_factory=list)  # every tool that really ran, in order
    notes: list[str] = field(default_factory=list)  # what tools left for the operator, in order
    recorded: bool = False  # the ledger has any entry at all

    @property
    def spent_usd(self) -> float:
        return round(sum(item.usd for item in self.items), 4)


def read_entries(directory: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with open(directory / LEDGER_FILENAME, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError:
        pass
    return entries


def _label(entry: dict[str, Any]) -> str:
    label = str(entry.get("tool") or "tool")
    if entry.get("model"):
        label += f" {entry['model']}"
    if entry.get("output_path"):
        label += f" ({Path(str(entry['output_path'])).name})"
    return label[:200]


def _usd(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and value > 0 else 0.0


def _same_output(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return bool(a.get("output_path")) and (a.get("tool"), a.get("output_path")) == (b.get("tool"), b.get("output_path"))


def summarize(entries: list[dict[str, Any]]) -> LedgerState:
    state = LedgerState(recorded=bool(entries))
    open_calls: dict[str, dict[str, Any]] = {}
    for entry in entries:
        event = entry.get("event")
        if event == "start":
            open_calls[str(entry.get("id"))] = entry
            state.tools_started.append(str(entry.get("tool")))
        elif event == "finish":
            open_calls.pop(str(entry.get("id")), None)
            if entry.get("note"):
                state.notes.append(str(entry["note"]))
            cost = _usd(entry.get("cost_usd"))
            if cost:
                rids = entry.get("request_ids")
                rids = tuple(str(r) for r in rids if r) if isinstance(rids, list) else ()
                state.items.append(CostItem(_label(entry), round(cost, 4), entry.get("provider"), rids))
            if entry.get("success"):
                for call_id, started in list(open_calls.items()):
                    if _same_output(started, entry):
                        del open_calls[call_id]
        elif event == "denied":
            state.denials.append(entry)
    for started in open_calls.values():
        estimate = _usd(started.get("estimate_usd"))
        if estimate:
            state.items.append(CostItem(f"{_label(started)} (not finished, estimated)", round(estimate, 4),
                                        started.get("provider")))
    return state


def _append(directory: Path, entry: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **entry}, default=str)
    with open(directory / LEDGER_FILENAME, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


# -- one tool call -----------------------------------------------------------------------
@dataclass
class ToolCall:
    id: str
    details: dict[str, Any]
    denied: str | None = None  # the message for the agent when the budget refuses this call
    estimate_usd: float | None = None


def begin_call(directory: Path, *, tool: str, provider: str | None, inputs: Any,
               estimate_usd: float | None, unpriced: bool = False) -> ToolCall:
    """Allow or deny one tool call, and note that it started: the job's stage follows what really
    ran. ``estimate_usd`` is None when the tool can't quote it: it then passes until the budget is
    used up, unless ``unpriced`` says the tool has no price for it at all, which is refused. A free
    call (estimate 0) always passes, so the wrap-up (mixing, rendering, exporting) can still run on
    a spent budget."""
    source = inputs if isinstance(inputs, dict) else {}
    details = {
        "tool": tool,
        "provider": provider,
        "model": source.get("model") or source.get("model_id"),
        "output_path": str(source["output_path"]) if source.get("output_path") else None,
    }
    call = ToolCall(id=uuid.uuid4().hex, details=details, estimate_usd=estimate_usd)
    if unpriced:
        call.denied = unpriced_message(tool)
        _append(directory, {"event": "denied", **details, "reason": "unpriced"})
        return call
    budget = read_budget(directory)
    if budget is not None:
        spent = summarize(read_entries(directory)).spent_usd
        over = spent >= budget if estimate_usd is None else estimate_usd > 0 and spent + estimate_usd > budget + 1e-9
        if over:
            call.denied = deny_message(spent, budget, tool, estimate_usd)
            _append(directory, {"event": "denied", **details, "estimate_usd": estimate_usd,
                                "spent_usd": spent, "budget_usd": budget})
            return call
    _append(directory, {"event": "start", "id": call.id, **details, "estimate_usd": estimate_usd or 0.0})
    return call


def finish_call(directory: Path, call: ToolCall, *, cost_usd: Any, success: Any,
                request_ids: Any = None, note: Any = None) -> None:
    """Record what the call really cost. A successful call that reports no cost is recorded at its
    quote: a paid call is never free. ``request_ids`` are the provider's ids for this call's
    submissions, kept so settlement can reconcile the estimate against real billing events.
    ``note`` is a sentence the tool left for the operator (it becomes a warning of the job)."""
    entry = {"event": "finish", "id": call.id, **call.details, "cost_usd": _usd(cost_usd), "success": bool(success)}
    if isinstance(note, str) and note.strip():
        entry["note"] = note.strip()[:400]
    if isinstance(request_ids, (list, tuple)):
        rids = [str(r) for r in request_ids if r]
        if rids:
            entry["request_ids"] = rids
    if cost_usd is None and success and _usd(call.estimate_usd):
        entry.update(cost_usd=_usd(call.estimate_usd), cost_source="quote")
    _append(directory, entry)
