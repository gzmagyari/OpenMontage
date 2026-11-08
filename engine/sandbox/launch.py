"""Start a program inside a job's box: ``python launch.py <spec.json> <program> [args...]``.

The SDK starts the Claude CLI through this (``ClaudeAgentOptions.cli_path`` is the job's
``box/claude`` script, which calls it), and the desk starts paid tools through it. Standard
library only, so it runs before anything else is imported. The spec, written outside the
box by ``engine/sandbox/box.py``, lists what the box contains; anything not listed stays
out, the inherited environment included.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Mapping

# Debian's merged /usr: these top-level names are symlinks into /usr.
_USR_LINKS = ("bin", "lib", "lib64", "sbin")


def build_argv(spec: Mapping[str, Any], environ: Mapping[str, str], command: list[str]) -> list[str]:
    argv = ["bwrap", "--die-with-parent", "--new-session",
            "--unshare-all", "--unshare-user", "--disable-userns"]
    if spec.get("share_net"):
        argv.append("--share-net")
    for path in spec.get("ro", []):
        if os.path.exists(path):
            argv += ["--ro-bind", path, path]
    for name in _USR_LINKS:
        link = "/" + name
        if os.path.islink(link):
            argv += ["--symlink", os.readlink(link), link]
    for source, target in spec.get("rw", []):
        argv += ["--bind", source, target]
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/dev/shm"]
    if spec.get("env_from_parent"):
        # The provider box: its environment is the one the desk started it with.
        env: dict[str, str] = {}
    else:
        argv.append("--clearenv")
        env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        for name in spec.get("pass", []):
            if name in environ:
                env[name] = environ[name]
        for name, source in (spec.get("rename") or {}).items():
            env[name] = environ.get(source, "")
    for name, value in sorted(env.items()):
        argv += ["--setenv", name, value]
    argv += ["--chdir", str(spec.get("chdir") or "/")]
    argv += ["--", *spec.get("init", []), *command]
    return argv


def main() -> None:
    if len(sys.argv) < 3:
        sys.exit("usage: launch.py <spec.json> <program> [args...]")
    with open(sys.argv[1], encoding="utf-8") as fh:
        spec = json.load(fh)
    argv = build_argv(spec, os.environ, sys.argv[2:])
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
