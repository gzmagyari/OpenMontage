"""The box side of the paid-tools desk. Standard library only (tools/base_tool.py imports it).

Inside an agent box, OPENMONTAGE_TOOL_DESK names the desk's socket. A call of a tool that
needs keys or the network goes there whole; the desk answers with the tool's result.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any

DESK_ENV = "OPENMONTAGE_TOOL_DESK"
# A paid call may wait on a provider for a long time (the Bash limit is 30 minutes).
_TIMEOUT_SECONDS = 35 * 60


def desk_socket() -> str | None:
    return os.environ.get(DESK_ENV) or None


def request(payload: dict[str, Any], *, timeout: float = _TIMEOUT_SECONDS) -> dict[str, Any]:
    path = desk_socket()
    if not path:
        raise RuntimeError("not inside an agent box")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(json.dumps(payload, default=str).encode("utf-8") + b"\n")
        sock.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    answer = json.loads(b"".join(chunks) or b"{}")
    if not isinstance(answer, dict):
        raise RuntimeError("the desk sent an unexpected answer")
    return answer
