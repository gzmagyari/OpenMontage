"""Run one paid tool call inside a provider box: request JSON on stdin, ToolResult JSON on stdout.

The desk starts this in a short-lived box that holds the provider keys and the network but
sees only the job's project folder. Anything the tool prints goes to stderr, so stdout
carries nothing but the result.
"""

from __future__ import annotations

import dataclasses
import importlib
import json
import sys
from pathlib import Path


def main() -> int:
    request = json.load(sys.stdin)
    result_out = sys.stdout
    sys.stdout = sys.stderr
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tools.base_tool import ToolResult

    try:
        module = importlib.import_module(request["module"])
        tool = getattr(module, request["class"])()
        result = tool.execute(request.get("inputs") or {})
        if not isinstance(result, ToolResult):
            result = ToolResult(success=False, error=f"{request['class']}.execute returned {type(result).__name__}")
    except Exception as exc:  # the tool's own failure, reported like any failed call
        result = ToolResult(success=False, error=f"{type(exc).__name__}: {exc}"[:4000])
    result_out.write(json.dumps(dataclasses.asdict(result), default=str) + "\n")
    result_out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
