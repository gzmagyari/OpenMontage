"""The budget-death wrap-up: a run ended by the gateway's model-call cap exports instead of failing."""

from __future__ import annotations

from engine.agent.runner import _WRAP_PROMPT, _llm_budget_death


class ResultError(Exception):
    """Stands in for claude_agent_sdk's ResultError; the matcher goes by name, not import."""


class OtherError(Exception):
    pass


def test_recognizes_the_gateway_budget_death():
    # The real exit from job_26fdeba34df9d136: the CLI read the gateway's 403 as an auth failure.
    exc = ResultError("Claude Code returned an error result: Failed to authenticate. API Error: "
                      "403 this job's Claude budget is used up; finish with what exists (exit code: 1)")
    assert _llm_budget_death(exc)


def test_ignores_other_result_errors_and_other_exception_types():
    assert not _llm_budget_death(ResultError("the tool harness crashed"))
    assert not _llm_budget_death(OtherError("this job's Claude budget is used up"))


def test_the_wrap_prompt_forbids_new_spending_and_demands_an_export():
    assert "Do not generate any new assets" in _WRAP_PROMPT
    assert "do not call any paid tool" in _WRAP_PROMPT
    assert "export" in _WRAP_PROMPT.lower()
    assert "RUN_REPORT.md" in _WRAP_PROMPT
