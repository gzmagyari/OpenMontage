"""Live money state for one agent run.

The budget itself is enforced on every tool call (lib/job_ledger.py, reached from
tools/base_tool.py): that is the one place a paid call can't avoid, however the agent
invokes the tool. A ``BudgetMeter`` is the runner's view of the run while it works:
paid-generation spend mirrored from the job's tool ledger, for the cost lines and the
watchdog, and the Claude-loop spend, metered live from per-message token usage and
replaced by the SDK's exact figure when the run ends. The SDK's own
``max_budget_usd`` already hard-caps Claude server-side, so it never counts against
the generation budget.
"""

from __future__ import annotations

import os
from typing import Any, Callable

# Claude token rates in USD per million tokens, for the LIVE estimate only (the
# exact figure from the SDK result message replaces it at the end). Haiku-class
# defaults; override per deployment when the model changes.
_RATES = {
    "input": float(os.getenv("BUDGET_LLM_INPUT_PER_MTOK", "1.0")),
    "output": float(os.getenv("BUDGET_LLM_OUTPUT_PER_MTOK", "5.0")),
    "cache_write": float(os.getenv("BUDGET_LLM_CACHE_WRITE_PER_MTOK", "1.25")),
    "cache_read": float(os.getenv("BUDGET_LLM_CACHE_READ_PER_MTOK", "0.10")),
}


class BudgetMeter:
    """One run's live money state. ``budget`` is a callable so a lowered
    job budget applies at once."""

    def __init__(self, budget: Callable[[], float]):
        self._budget = budget
        self.actuals_total = 0.0
        self.llm_estimate = 0.0
        self.llm_exact: float | None = None

    def budget(self) -> float:
        try:
            return max(0.0, float(self._budget() or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def generation_spend(self) -> float:
        return self.actuals_total

    def remaining(self) -> float:
        return self.budget() - self.generation_spend()

    def add_usage(self, usage: dict[str, Any] | None) -> float:
        """Fold one assistant message's token usage into the live LLM estimate."""
        if not isinstance(usage, dict):
            return self.llm_spend()
        tokens = {
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "cache_write": usage.get("cache_creation_input_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"),
        }
        for kind, count in tokens.items():
            if isinstance(count, (int, float)) and count > 0:
                self.llm_estimate += float(count) * _RATES[kind] / 1_000_000.0
        return self.llm_spend()

    def llm_spend(self) -> float:
        return round(self.llm_exact if self.llm_exact is not None else self.llm_estimate, 4)
