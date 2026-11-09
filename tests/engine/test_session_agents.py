"""The delegation surface: Task is allowed, the workers are defined, nothing escapes the turn."""

from __future__ import annotations

import inspect

from engine.agent.session import AGENT_TOOLS, MECHANIC_AGENT, AgentSession, composer_agent


def test_task_joined_the_tool_allowlist():
    assert "Task" in AGENT_TOOLS
    # The turn-escaping surfaces stay out.
    for tool in ("TodoWrite", "KillShell", "NotebookEdit"):
        assert tool not in AGENT_TOOLS


def test_the_mechanic_is_a_foreground_haiku_worker():
    assert MECHANIC_AGENT.model == "claude-haiku-5-5"
    assert set(MECHANIC_AGENT.tools) == {"Bash", "Read", "Write", "Edit"}
    # Foreground only: the definition never asks for background execution.
    assert not getattr(MECHANIC_AGENT, "background", None)
    assert "Never call paid" in MECHANIC_AGENT.prompt or "never call paid" in MECHANIC_AGENT.prompt.lower()


def test_the_composer_is_a_creative_coauthor_on_its_own_model():
    agent = composer_agent()
    assert agent.model == "claude-sonnet-5-5"          # the deployment default
    assert composer_agent("claude-opus-5-5").model == "claude-opus-5-5"  # the env knob
    assert set(agent.tools) == {"Bash", "Read", "Write", "Edit"}
    assert not getattr(agent, "background", None)
    low = agent.prompt.lower()
    assert "never call paid" in low
    assert "never a generic template" in low            # creative code, not a pipeline
    assert "composition/src" in agent.prompt
    assert "absolute project path" in low               # probe finding: /app cwd is read-only


def test_the_session_takes_the_composer_model_knob():
    assert "composer_model" in inspect.signature(AgentSession.__init__).parameters
