"""One agent session's box: its three sockets, its description and the launcher the SDK runs.

``async with AgentBox(...) as box`` starts the job's gateway, web proxy and desk, writes
``<agent dir>/box/spec.json`` and the ``claude`` launcher, and stops them all on the way
out. The session then runs with ``cli_path=box.cli_path``: the SDK starts the launcher,
the launcher builds the box (``launch.py``), and the CLI runs inside it.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from engine.sandbox.desk import ToolDesk
from engine.sandbox.gateway import ClaudeGateway
from engine.sandbox.webproxy import WebProxy

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCH = Path(__file__).with_name("launch.py")
BOX_INIT = Path(__file__).with_name("box_init.sh")
BOX_SOCKETS = "/run/openmontage"
GATEWAY_PORT = 41100
WEB_PORT = 41101
# The SDK adds these to the CLI's environment; the box keeps exactly these from it.
SDK_ENV = ("CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_SDK_READS_SESSION_STATE",
           "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING")
# Names that hold credentials. Their values never enter an agent box.
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|SECRETKEY|ACCESSKEY|PASSWORD|CREDENTIALS)$", re.I)
_SERVER_ONLY = {"OPENMONTAGE_API_TOKEN", "GPG_KEY", "ANTHROPIC_API_KEY"}
# Small models the CLI itself uses for side tasks (WebFetch summaries and the like).
SIDE_MODEL_PREFIXES = ("claude-haiku-",)


class SandboxUnavailable(Exception):
    """This host cannot build agent boxes."""


@functools.lru_cache(maxsize=1)
def sandbox_problem() -> str | None:
    """None when this host can build boxes; otherwise why not (checked once per process)."""
    if sys.platform != "linux":
        return "agent boxes need Linux"
    for program in ("bwrap", "socat"):
        if shutil.which(program) is None:
            return f"{program} is not installed"
    probe = ["bwrap", "--unshare-all", "--unshare-user", "--disable-userns", "--ro-bind", "/", "/",
             "--proc", "/proc", "--dev", "/dev", "true"]
    try:
        done = subprocess.run(probe, capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"bubblewrap could not start: {exc}"
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip()[:300]
        return ("bubblewrap cannot create a box in this container (start it with "
                "--security-opt seccomp=docker/seccomp-openmontage.json --security-opt systempaths=unconfined "
                f"--security-opt apparmor=unconfined): {detail}")
    return None


def bundled_cli() -> str:
    import claude_agent_sdk

    return str(Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude")


def secret_names(environ: dict[str, str] | os._Environ) -> list[str]:
    """Provider credentials this server holds (names only), for the box's placeholders."""
    return sorted(name for name, value in environ.items()
                  if value and _SECRET_NAME.search(name) and name not in _SERVER_ONLY)


def provider_env(environ: dict[str, str] | os._Environ, shared_tmp: str = "/tmp") -> dict[str, str]:
    """A provider box's environment: the server's provider settings and keys, nothing that
    drives the server or the agent."""
    env = {name: value for name, value in environ.items()
           if not name.startswith(("CLAUDE", "OM_BOX_")) and name not in {"OPENMONTAGE_API_TOKEN",
                                                                         "OPENMONTAGE_TOOL_DESK",
                                                                         "OPENMONTAGE_JOB_LEDGER_DIR"}}
    env.update(HOME=f"{shared_tmp}/.provider-home", TMPDIR=shared_tmp, PYTHONUNBUFFERED="1",
               PYTHONDONTWRITEBYTECODE="1")
    if Path("/opt/hf").is_dir():
        env.update(HF_HOME="/opt/hf", HF_HUB_OFFLINE="1")
    return env


class AgentBox:
    def __init__(self, *, agent_dir: Path, project_dir: Path, model: str, claude_cap_usd: float,
                 cli_switches: dict[str, str], api_key: str, warn: Callable[[str], None] | None = None,
                 gateway_transport=None):
        self.agent_dir = Path(agent_dir)
        self.project_dir = Path(project_dir)
        self.box_dir = self.agent_dir / "box"
        self.sockets = self.box_dir / "sockets"
        self.tmp = self.box_dir / "tmp"
        self.spec_path = self.box_dir / "spec.json"
        self.cli_path = str(self.box_dir / "claude")
        self.model = model
        self.claude_cap_usd = claude_cap_usd
        self.cli_switches = dict(cli_switches)
        self._api_key = api_key
        self._warn = warn or (lambda _message: None)
        self._token = secrets.token_urlsafe(32)
        self._gateway_transport = gateway_transport  # checks only (scripts/agent_box_check.py)
        self.gateway: ClaudeGateway | None = None
        self.web: WebProxy | None = None
        self.desk: ToolDesk | None = None

    # The SDK's environment for the launcher: only the job's token. Everything else the box
    # gets is written in its spec; the launcher drops the rest of what it inherits.
    def session_env(self) -> dict[str, str]:
        return {"OM_BOX_CLAUDE_TOKEN": self._token}

    @property
    def settings_json(self) -> str:
        # WebFetch's domain pre-check calls Anthropic directly; the web proxy already limits
        # the box to public sites.
        return json.dumps({"skipWebFetchPreflight": True})

    def _box_env(self) -> dict[str, str]:
        proxy = f"http://127.0.0.1:{WEB_PORT}"
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "HOME": "/tmp/home",
            "TMPDIR": "/tmp",
            "PWD": str(REPO_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "OPENMONTAGE_DATA_DIR": os.environ.get("OPENMONTAGE_DATA_DIR", "/data"),
            "OPENMONTAGE_PROJECTS_DIR": str(self.project_dir.parent),
            "CLAUDE_CONFIG_DIR": str(self.project_dir / ".agent_session"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{GATEWAY_PORT}",
            "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
            "OPENMONTAGE_TOOL_DESK": f"{BOX_SOCKETS}/desk.sock",
            "OM_BOX_GATEWAY_PORT": str(GATEWAY_PORT),
            "OM_BOX_WEB_PORT": str(WEB_PORT),
            **self.cli_switches,
        }
        if Path("/opt/hf").is_dir():
            env.update(HF_HOME="/opt/hf", HF_HUB_OFFLINE="1")
        # Tools check that their provider is configured; the desk holds the real values.
        for name in secret_names(os.environ):
            env[name] = "held-by-the-desk"
        return env

    def _spec(self) -> dict:
        return {
            "ro": ["/usr", "/etc", str(REPO_ROOT), "/opt"],
            "rw": [[str(self.project_dir), str(self.project_dir)], [str(self.tmp), "/tmp"],
                   [str(self.sockets), BOX_SOCKETS]],
            "env": self._box_env(),
            "pass": list(SDK_ENV),
            "rename": {"ANTHROPIC_API_KEY": "OM_BOX_CLAUDE_TOKEN"},
            "chdir": str(REPO_ROOT),
            "init": ["/bin/sh", str(BOX_INIT)],
        }

    async def __aenter__(self) -> "AgentBox":
        problem = await asyncio.to_thread(sandbox_problem)
        if problem:
            raise SandboxUnavailable(problem)
        for path in (self.sockets, self.tmp / "home"):
            path.mkdir(parents=True, exist_ok=True)
        for stale in self.sockets.glob("*.sock"):
            stale.unlink(missing_ok=True)
        self.gateway = ClaudeGateway(
            socket_path=self.sockets / "gateway.sock", token=self._token, api_key=self._api_key,
            models={self.model}, model_prefixes=SIDE_MODEL_PREFIXES, cap_usd=self.claude_cap_usd,
            transport=self._gateway_transport,
            on_refusal=lambda reason: self._warn(f"the Claude gateway refused a request ({reason})"))
        self.web = WebProxy(socket_path=self.sockets / "web.sock", log_path=self.agent_dir / "web.log")
        self.desk = ToolDesk(socket_path=self.sockets / "desk.sock", ledger_dir=self.agent_dir,
                             project_dir=self.project_dir, shared_tmp=self.tmp, specs_dir=self.box_dir / "calls",
                             provider_env=provider_env(os.environ), log_path=self.agent_dir / "desk.log")
        try:
            await self.gateway.start()
            await self.web.start()
            await self.desk.start()
            self.spec_path.write_text(json.dumps(self._spec(), indent=1), encoding="utf-8")
            launcher = Path(self.cli_path)
            launcher.write_text(
                "#!/bin/sh\n# Starts this job's Claude CLI inside its box (engine/sandbox/box.py).\n"
                f'exec "{sys.executable}" "{LAUNCH}" "{self.spec_path}" "{bundled_cli()}" "$@"\n',
                encoding="utf-8")
            launcher.chmod(0o755)
        except BaseException:
            await self._stop()
            raise
        return self

    async def __aexit__(self, *_exc) -> None:
        await self._stop()

    async def _stop(self) -> None:
        for server in (self.desk, self.web, self.gateway):
            if server is not None:
                try:
                    await server.close()
                except Exception:
                    pass
        for sock in self.sockets.glob("*.sock"):
            sock.unlink(missing_ok=True)
