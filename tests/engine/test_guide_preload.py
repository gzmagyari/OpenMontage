"""The guide's preloaded skill digests stay verbatim copies of their source files.

AGENT_GUIDE.md tells the director to read the preloaded text instead of spending turns
on the source files, so the copies must never drift from the sources.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

PRELOADED_SOURCES = [
    REPO / ".agents" / "skills" / "remotion-best-practices" / "rules" / "videos.md",
    REPO / ".agents" / "skills" / "remotion-best-practices" / "rules" / "assets.md",
    REPO / ".agents" / "skills" / "framer-motion" / "SKILL.md",
]


def _body(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if text.startswith("---"):
        end = text.find("\n---", 3)
        assert end != -1, f"unterminated frontmatter in {path}"
        text = text[end + len("\n---"):]
    return text.strip()


def test_every_preloaded_skill_appears_verbatim_in_the_guide():
    guide = (REPO / "AGENT_GUIDE.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    for source in PRELOADED_SOURCES:
        assert _body(source) in guide, (
            f"{source.relative_to(REPO)} changed but its preloaded copy in AGENT_GUIDE.md "
            "(Preloaded Skill Digests) was not updated"
        )


def test_the_guide_sends_the_agent_to_the_preflight_menu_file():
    guide = (REPO / "AGENT_GUIDE.md").read_text(encoding="utf-8")
    assert "work/provider_menu.json" in guide
    assert "Latency Rules (Binding)" in guide
