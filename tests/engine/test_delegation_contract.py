"""The parallel-production contract lives in the stage skills' definition of done.

The #711 lesson: a polite global guide section was ignored (zero Task calls). The
contract is therefore written into the stage skills and the checkpoint protocol, and
these tests keep every binding phrase in place.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SPOKE = REPO / "skills" / "pipelines" / "avatar-spokesperson"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_the_guide_defines_the_one_message_spawn_and_both_workers():
    guide = _read(REPO / "AGENT_GUIDE.md")
    assert "The one-message spawn (MUST, at script lock" in guide
    assert "`composer`" in guide and "`mechanic`" in guide
    assert "never poll" in guide.lower()
    flat = " ".join(guide.split())
    assert "authoring the first draft solo is a protocol violation" in flat


def test_script_lock_demands_the_shopping_list_and_the_spawn():
    text = _read(SPOKE / "script-director.md")
    assert "## Script Lock in Headless Jobs (Binding)" in text
    assert "metadata.shopping_list" in text
    assert "one-message spawn" in text.lower()
    assert "hook plus 1-2 body takes" in text  # take discipline


def test_the_burst_is_bound_to_the_shopping_list():
    text = _read(SPOKE / "asset-director.md")
    assert "### Shopping list (Binding)" in text
    assert "buys exactly" in text
    assert "Never poll" in text


def test_the_composition_first_draft_belongs_to_the_composer():
    edit = _read(SPOKE / "edit-director.md")
    compose = _read(SPOKE / "compose-director.md")
    assert "first full draft" in edit and "composer" in edit
    assert "delegation record" in compose and "INVALID" in compose


def test_the_checkpoints_require_both_records():
    protocol = _read(REPO / "skills" / "meta" / "checkpoint-protocol.md")
    assert "metadata.shopping_list" in protocol
    assert "metadata.delegation" in protocol
    assert "no composer entry" in protocol
    # The preloaded digest in the guide carries the same requirement.
    guide = _read(REPO / "AGENT_GUIDE.md")
    assert "metadata.shopping_list" in guide
    assert "metadata.delegation" in guide


def test_the_reports_reconcile_delegation_and_spend():
    ep = _read(SPOKE / "executive-producer.md")
    pub = _read(SPOKE / "publish-director.md")
    assert "metadata.shopping_list" in ep and "metadata.delegation" in ep
    assert "delegation record" in pub
    assert "never show cost, margin or multipliers" in pub
