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


def test_the_composer_writes_custom_scenes_only_on_its_own_model():
    agent = composer_agent()
    assert agent.model == "claude-sonnet-5-5"          # the deployment default
    assert composer_agent("claude-opus-5-5").model == "claude-opus-5-5"  # the env knob
    assert set(agent.tools) == {"Bash", "Read", "Write", "Edit"}
    assert not getattr(agent, "background", None)
    low = " ".join(agent.prompt.lower().split())
    assert "never call paid" in low
    assert "never a generic template" in low            # creative code, not a pipeline
    assert "composition/src/scenes/" in low             # scenes only: the kit assembles the video
    assert "do not touch composition/edit.json" in low  # the edit is the director's
    assert "absolute project path" in low               # probe finding: /app cwd is read-only
    assert "only when the plan lists custom scenes" in " ".join(agent.description.lower().split())


def test_the_mechanic_no_longer_does_composition_chores():
    # Staging, word timing and the mix are kit commands now.
    assert "composition chores" not in MECHANIC_AGENT.prompt.lower()
    assert "composition chores" not in MECHANIC_AGENT.description.lower()


def test_the_session_takes_the_composer_model_knob():
    assert "composer_model" in inspect.signature(AgentSession.__init__).parameters
