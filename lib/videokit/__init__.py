"""The video kit's command and its Python half.

The kit turns a short edit file into a rendered video: the engine, starter looks and
ready-made scenes live in ``remotion-composer/kit``; this package stages a job's workspace,
prepares the voice (trim, word timings, the timing spine), builds the soundtrack, and
forwards check / sheet / render to the Node side.

One command, run in a project directory:
``kit init | look | voice | check | sheet | mix | render | cover``.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
KIT_DIR = REPO_ROOT / "remotion-composer" / "kit"
