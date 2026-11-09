"""Refresh AGENT_GUIDE.md's preloaded copy of the video kit's reference.

The guide ends with a verbatim copy of remotion-composer/kit/README.md so the director never
spends a turn reading it. Run this after changing the README:

    python scripts/sync_kit_digest.py
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HEADING = "### The video kit (verbatim: `remotion-composer/kit/README.md`)"


def main() -> int:
    guide_path = REPO / "AGENT_GUIDE.md"
    guide = guide_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    reference = (REPO / "remotion-composer" / "kit" / "README.md").read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    start = guide.index(HEADING)
    updated = guide[:start] + HEADING + "\n\n" + reference + "\n"
    if updated != guide:
        guide_path.write_text(updated, encoding="utf-8", newline="\n")
        print("AGENT_GUIDE.md: the kit reference was refreshed")
    else:
        print("AGENT_GUIDE.md: already in sync")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
