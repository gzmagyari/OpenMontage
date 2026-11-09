"""Model-aware gateway pricing: side-model (Haiku) traffic meters at its own rates."""

from __future__ import annotations

import inspect

from engine.agent.budget import BudgetMeter
from engine.sandbox.gateway import ClaudeGateway, _HAIKU_RATES, _rates_for


def test_rates_for_picks_the_side_model_table():
    assert _rates_for("claude-haiku-5-5") is _HAIKU_RATES
    assert _rates_for("claude-haiku-4-5") is _HAIKU_RATES
    assert _rates_for("gpt-6.1-terra-fast") is None
    assert _rates_for("claude-opus-5-5") is None
    assert _rates_for(None) is None


def test_the_forward_path_accepts_the_request_model():
    assert "model" in inspect.signature(ClaudeGateway._forward).parameters


def test_meter_prices_a_call_at_the_given_rates():
    meter = BudgetMeter(lambda: 0.0)
    # 1M output tokens at Haiku's $0.50/MTok, not the deployment default.
    meter.add_usage({"output_tokens": 1_000_000}, "msg_haiku", rates=_HAIKU_RATES)
    assert meter.llm_spend() == 0.50
    # The default table still applies when no rates are passed (env default output $1.50).
    meter2 = BudgetMeter(lambda: 0.0)
    meter2.add_usage({"output_tokens": 1_000_000}, "msg_main")
    assert meter2.llm_spend() > 0.50 or meter2.llm_spend() != 0.50


def test_rates_do_not_break_message_dedupe():
    meter = BudgetMeter(lambda: 0.0)
    meter.add_usage({"output_tokens": 1_000_000}, "same_id", rates=_HAIKU_RATES)
    meter.add_usage({"output_tokens": 1_000_000}, "same_id", rates=_HAIKU_RATES)
    assert meter.llm_spend() == 0.50
