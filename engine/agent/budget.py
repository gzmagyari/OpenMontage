"""Live paid-generation accounting and enforcement for one agent run.

The brief has always told the agent its budget; this module makes the server
hold it to that. Three pieces:

- A ``BudgetMeter``: the run's live money state. Generation spend is tracked
  two ways and the larger wins: floor estimates committed the moment a paid
  call is ALLOWED (so spend can never be hidden by a tool that prints no
  cost), and the actual ``cost_usd`` figures parsed out of tool results. The
  Claude-loop spend is metered live from per-message token usage and replaced
  by the SDK's exact figure when the run ends; the SDK's own
  ``max_budget_usd`` already hard-caps it server-side, so it never counts
  against the generation budget.
- A PreToolUse hook: when the remaining generation budget cannot cover a paid
  call's floor estimate, the call is DENIED with an instruction to stop
  buying and export the best cut from what already exists. The job then
  finishes normally and honestly degraded, which is what the brief's
  degrade-gracefully rule always promised.
- ``parse_costs``: the paid tools print ``"cost_usd": <n>`` in their JSON
  output (see tools/avatar/fal_avatar.py), so the server reads spend from the
  tool results themselves rather than trusting the agent's bookkeeping.

The budget is read through a callable on every decision, so lowering a
running job's ``options.max_budget_usd`` (POST /v1/jobs/{id}/budget) clamps
the run from the next paid call on.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable

# Floor estimates in USD for one call of each paid kind: deliberately below the
# typical real price (an avatar take measures $0.40-0.90, a music track $0.60),
# so an allowed call is never blocked by an overestimate, while a tool that
# reports nothing still moves the meter.
_PAID: list[tuple[re.Pattern[str], str, float]] = [
    (re.compile(r"piper_tts|audio_probe|audio_energy|composition_validator|cost_tracker", re.I), "free", 0.0),
    (re.compile(r"tools[/\\]avatar[/\\]|lip_sync|talking_head", re.I), "avatar", 0.40),
    (re.compile(r"elevenlabs_music|elevenlabs_soundtrack|suno|google_music|comfyui_music", re.I), "music", 0.50),
    (re.compile(r"freesound_music", re.I), "music", 0.02),
    (re.compile(r"[a-z0-9_]*_tts\.py|\btts\b", re.I), "tts", 0.005),
    (re.compile(r"tools[/\\](video|_kling|_comfyui)[/\\]|kling|veo|seedance|minimax|hailuo|higgsfield|grok_video", re.I),
     "video", 0.25),
    (re.compile(r"tools[/\\](graphics|character|enhancement)[/\\]|flux|ideogram|recraft|gpt_image|imagen|qwen_image|grok_image|hunyuan", re.I),
     "image", 0.03),
    (re.compile(r"azure_stt|dashscope_asr|mistral_stt", re.I), "stt", 0.005),
    (re.compile(r"fal_media|provider_jobs", re.I), "provider", 0.10),
]

_COST_USD = re.compile(r'"cost_usd"\s*:\s*([0-9]+(?:\.[0-9]+)?)')

# Claude token rates in USD per million tokens, for the LIVE estimate only (the
# exact figure from the SDK result message replaces it at the end). Haiku-class
# defaults; override per deployment when the model changes.
_RATES = {
    "input": float(os.getenv("BUDGET_LLM_INPUT_PER_MTOK", "1.0")),
    "output": float(os.getenv("BUDGET_LLM_OUTPUT_PER_MTOK", "5.0")),
    "cache_write": float(os.getenv("BUDGET_LLM_CACHE_WRITE_PER_MTOK", "1.25")),
    "cache_read": float(os.getenv("BUDGET_LLM_CACHE_READ_PER_MTOK", "0.10")),
}


def classify(tool_name: str, tool_input: dict[str, Any] | None) -> tuple[str, float] | None:
    """(kind, floor_usd) for a paid call, None for a free one.

    Paid work runs through Bash invocations of the repo's tools; the command
    string (or a non-Bash tool's own name) is matched against the registry.
    Unmatched commands are free: their printed cost_usd still lands in the
    meter, and the NEXT paid call is then judged against the updated spend.
    """
    haystack = str((tool_input or {}).get("command", "")) if tool_name == "Bash" else tool_name
    if "tools" not in haystack and tool_name == "Bash":
        return None
    for pattern, kind, floor in _PAID:
        if pattern.search(haystack):
            return None if kind == "free" else (kind, floor)
    return None


def parse_costs(text: str) -> list[float]:
    """Every ``"cost_usd": <n>`` a tool printed, sanity-capped."""
    return [value for value in (float(m) for m in _COST_USD.findall(text or "")) if 0 < value <= 500]


class BudgetMeter:
    """One run's live money state. ``budget`` is a callable so a lowered
    job budget takes effect on the next decision."""

    def __init__(self, budget: Callable[[], float]):
        self._budget = budget
        self.floors_total = 0.0
        self.actuals_total = 0.0
        self.llm_estimate = 0.0
        self.llm_exact: float | None = None
        self.denials = 0

    def budget(self) -> float:
        try:
            return max(0.0, float(self._budget() or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def generation_spend(self) -> float:
        return max(self.actuals_total, self.floors_total)

    def remaining(self) -> float:
        return self.budget() - self.generation_spend()

    def commit_floor(self, floor: float) -> None:
        self.floors_total = round(self.floors_total + floor, 4)

    def add_actual(self, usd: float) -> None:
        self.actuals_total = round(self.actuals_total + usd, 4)

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


def deny_message(meter: BudgetMeter, kind: str, floor: float) -> str:
    return (
        f"PAID-GENERATION BUDGET REACHED: ${meter.generation_spend():.2f} of the "
        f"${meter.budget():.2f} budget is spent and this {kind} call needs about ${floor:.2f} more. "
        "Buy nothing further: no retries of this call, no other paid tools. Finish the video NOW "
        "from the assets that already exist - assemble, render and export the best cut, then write "
        "RUN_REPORT.md noting what the budget could not cover."
    )


def make_hooks(meter: BudgetMeter, on_deny: Callable[[str, float], None] | None = None) -> dict:
    """SDK hook configuration enforcing the generation budget on Bash calls."""
    from claude_agent_sdk import HookMatcher

    async def pre_tool(input_data, _tool_use_id, _context) -> Any:  # HookCallback signature
        paid = classify(str(input_data.get("tool_name") or ""), input_data.get("tool_input") or {})
        if not paid:
            return {}
        kind, floor = paid
        if meter.remaining() < floor:
            meter.denials += 1
            if on_deny:
                on_deny(kind, floor)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": deny_message(meter, kind, floor),
            }}
        meter.commit_floor(floor)
        return {}

    return {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[pre_tool])]}
