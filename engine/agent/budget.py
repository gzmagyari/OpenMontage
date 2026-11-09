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

# Rates in USD per million tokens for the LIVE estimate only (the exact figure from
# the SDK result message replaces it at the end). Not list prices: fitted to real
# Haiku jobs, within 5% of the exact cost on finished runs. A call's output tokens
# aren't reported until the run ends, so the cache rates also carry that cost.
# Override per deployment when the model changes.
_RATES = {
    "input": float(os.getenv("BUDGET_LLM_INPUT_PER_MTOK", "0.30")),
    "output": float(os.getenv("BUDGET_LLM_OUTPUT_PER_MTOK", "1.50")),
    "cache_write": float(os.getenv("BUDGET_LLM_CACHE_WRITE_PER_MTOK", "0.35")),
    "cache_read": float(os.getenv("BUDGET_LLM_CACHE_READ_PER_MTOK", "0.05")),
}


class BudgetMeter:
    """One run's live money state. ``budget`` is a callable so a lowered
    job budget applies at once."""

    def __init__(self, budget: Callable[[], float]):
        self._budget = budget
        self.actuals_total = 0.0
        self.llm_estimate = 0.0
        self.llm_exact: float | None = None
        self._seen_messages: set[str] = set()

    def budget(self) -> float:
        try:
            return max(0.0, float(self._budget() or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def generation_spend(self) -> float:
        return self.actuals_total

    def remaining(self) -> float:
        return self.budget() - self.generation_spend()

    def add_usage(self, usage: dict[str, Any] | None, message_id: str | None = None,
                  rates: dict[str, float] | None = None) -> float:
        """Fold one API call's token usage into the live LLM estimate. The stream repeats a
        call's usage on every content block it emits, so a message id counts once. ``rates``
        prices this one call at other per-MTok rates (the gateway passes the side model's own
        rates for claude-haiku-* traffic); the default is the deployment's main-model rates."""
        if not isinstance(usage, dict) or message_id in self._seen_messages:
            return self.llm_spend()
        if message_id:
            self._seen_messages.add(message_id)
        table = rates or _RATES
        tokens = {
            "input": usage.get("input_tokens"),
            "output": usage.get("output_tokens"),
            "cache_write": usage.get("cache_creation_input_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"),
        }
        for kind, count in tokens.items():
            if isinstance(count, (int, float)) and count > 0:
                self.llm_estimate += float(count) * table[kind] / 1_000_000.0
        return self.llm_spend()

    def llm_spend(self) -> float:
        return round(self.llm_exact if self.llm_exact is not None else self.llm_estimate, 4)
