"""The delegation surface: Task is allowed, the mechanic is Haiku, and nothing escapes the turn."""

from __future__ import annotations

from engine.agent.session import AGENT_TOOLS, MECHANIC_AGENT


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
