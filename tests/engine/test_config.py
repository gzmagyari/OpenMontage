"""Settings parsing: the agent-effort knob."""

from __future__ import annotations

import pytest

from engine.config import load_settings


def test_agent_effort_defaults_to_the_model_default(monkeypatch):
    monkeypatch.delenv("OPENMONTAGE_AGENT_EFFORT", raising=False)
    assert load_settings().agent_effort is None


def test_agent_effort_accepts_a_level_case_insensitively(monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_AGENT_EFFORT", "High")
    assert load_settings().agent_effort == "high"


def test_agent_effort_refuses_a_typo_at_startup(monkeypatch):
    # A silently ignored typo would run every job at the default effort while the operator
    # believes they raised it; fail loudly instead.
    monkeypatch.setenv("OPENMONTAGE_AGENT_EFFORT", "hgih")
    with pytest.raises(ValueError, match="OPENMONTAGE_AGENT_EFFORT"):
        load_settings()
