"""The unattended brief: what every video job's agent is told before it starts."""

from __future__ import annotations

from pathlib import Path

from engine.agent.brief import build_brief


def test_the_brief_asks_for_plain_progress_notes():
    # Clients show the agent's messages live as a progress feed; a run that only thinks and calls
    # tools leaves that feed empty for the whole production.
    brief = build_brief(prompt="An ad for a cold brew", references=[], project_dir=Path("/data/projects/job_x"),
                        budget_usd=3.0)
    notes = brief.split("## Progress notes", 1)[1].split("\n## ", 1)[0]
    assert "Before\neach step, write one short sentence" in notes
    assert "file names, tool and model names, commands and costs" in notes
