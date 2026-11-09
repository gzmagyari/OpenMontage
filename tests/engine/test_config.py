"""Settings parsing: the agent-effort and LLM-upstream knobs."""

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


def test_llm_upstream_defaults_to_the_anthropic_api(monkeypatch):
    monkeypatch.delenv("OPENMONTAGE_AGENT_LLM_UPSTREAM", raising=False)
    monkeypatch.delenv("OPENMONTAGE_AGENT_LLM_UPSTREAM_KEY", raising=False)
    settings = load_settings()
    assert settings.agent_llm_upstream is None
    assert settings.agent_llm_upstream_key is None


def test_llm_upstream_takes_a_gateway_url_and_key(monkeypatch):
    # An Anthropic-format gateway (e.g. LiteLLM serving /v1/messages) standing in for the
    # Anthropic API, so the director can run on a non-Anthropic model with no box changes.
    monkeypatch.setenv("OPENMONTAGE_AGENT_LLM_UPSTREAM", "http://openmontage-litellm:4000/")
    monkeypatch.setenv("OPENMONTAGE_AGENT_LLM_UPSTREAM_KEY", "sk-virtual")
    settings = load_settings()
    assert settings.agent_llm_upstream == "http://openmontage-litellm:4000"
    assert settings.agent_llm_upstream_key == "sk-virtual"


def test_llm_upstream_refuses_half_a_configuration(monkeypatch):
    # A URL without its key would send every request with unusable auth; fail at startup,
    # not on the first job.
    monkeypatch.setenv("OPENMONTAGE_AGENT_LLM_UPSTREAM", "http://openmontage-litellm:4000")
    monkeypatch.delenv("OPENMONTAGE_AGENT_LLM_UPSTREAM_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENMONTAGE_AGENT_LLM_UPSTREAM"):
        load_settings()


def test_llm_upstream_refuses_a_non_http_value(monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_AGENT_LLM_UPSTREAM", "openmontage-litellm:4000")
    monkeypatch.setenv("OPENMONTAGE_AGENT_LLM_UPSTREAM_KEY", "sk-virtual")
    with pytest.raises(ValueError, match="http"):
        load_settings()


def test_composer_model_defaults_to_sonnet(monkeypatch):
    monkeypatch.delenv("OPENMONTAGE_COMPOSER_MODEL", raising=False)
    assert load_settings().composer_model == "claude-sonnet-5-5"


def test_composer_model_is_an_env_knob(monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_COMPOSER_MODEL", "claude-opus-5-5")
    assert load_settings().composer_model == "claude-opus-5-5"
