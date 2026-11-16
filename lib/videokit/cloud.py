"""`kit render`'s way to the cloud draw: `python -m lib.videokit.cloud <project> <name> [seconds]`.

The kit's render command runs in node inside the job's box, where there is no network. This
calls the `cloud_render` tool the way every paid tool is called, so in a job the call goes
through the paid-tools desk (budget, ledger, a provider box), and outside a job it simply runs.
One JSON line is printed: {"ok": true, ...the draw's facts} or {"ok": false, "error": ...}.
Exit 0 only when renders/<name>_raw.mp4 was written.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def draw(project: Path, name: str, seconds: float | None = None) -> dict:
    from tools.video.cloud_render import CloudRender

    inputs: dict = {"project_path": str(project), "name": name}
    if seconds:
        inputs["seconds"] = seconds
    result = CloudRender().execute(inputs)
    facts = dict(getattr(result, "data", None) or {})
    facts["ok"] = bool(getattr(result, "success", False))
    if not facts["ok"]:
        facts["error"] = str(getattr(result, "error", None) or facts.get("error") or "the cloud draw failed")
    return facts


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        print(json.dumps({"ok": False, "error": "usage: python -m lib.videokit.cloud <project> <name> [seconds]"}))
        return 2
    try:
        seconds = float(args[2]) if len(args) > 2 else None
    except ValueError:
        seconds = None
    try:
        facts = draw(Path(args[0]).resolve(), args[1], seconds)
    except Exception as exc:  # the caller falls back to a local draw; it needs a line, not a trace
        facts = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(facts, default=str))
    return 0 if facts.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
