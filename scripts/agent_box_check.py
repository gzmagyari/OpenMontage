#!/usr/bin/env python3
"""Check a job's agent box on this host. Run it after every image rebuild and container change.

No model and no paid tool: a real box is built the way a job builds one (engine/sandbox/box.py),
with a stand-in Claude upstream, and a probe inside it reports what it can see and reach. It
passes when the box holds no credential and nothing of the server, sees only its own project,
cannot write the repository, has no network of its own, and reaches Claude, the desk and the
web proxy only on the box's terms.

    docker exec -w /app openmontage-api python scripts/agent_box_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from engine.sandbox.box import LAUNCH, AgentBox, sandbox_problem, secret_names  # noqa: E402
from lib import job_ledger  # noqa: E402

MODEL = "claude-haiku-5-5"
STAND_IN_KEY = "stand-in-anthropic-key-outside-the-box"
MARKER = "OM_CHECK_PROVIDER_KEY"

PROBE = r'''
import ctypes, json, os, socket, subprocess, urllib.error, urllib.request

def attempt(fn):
    try:
        return fn()
    except Exception as exc:
        return f"error: {type(exc).__name__}"

report = {"env": dict(os.environ), "marker": os.environ.get("MARKER_NAME"),
          "claude_key": os.environ.get("ANTHROPIC_API_KEY")}
pids = [p for p in os.listdir("/proc") if p.isdigit()]
report["processes"] = sorted(open(f"/proc/{p}/comm").read().strip() for p in pids)
project = os.environ["OM_CHECK_PROJECT"]
data_root = "/" + project.strip("/").split("/")[0]
report["data_top"] = sorted(os.listdir(data_root))
report["projects"] = sorted(os.listdir(os.path.dirname(project)))
report["repo_write"] = attempt(lambda: open(os.environ["OM_CHECK_REPO"] + "/box_check_write", "w") and "written")
report["project_write"] = attempt(lambda: open(project + "/box_check_write", "w").write("ok") and "written")
report["tmp_write"] = attempt(lambda: open("/tmp/box_check_write", "w").write("ok") and "written")
report["direct_network"] = attempt(lambda: socket.create_connection(("1.1.1.1", 443), timeout=3) and "connected")

def claude(token, model):
    request = urllib.request.Request("http://127.0.0.1:41100/v1/messages", method="POST",
                                     data=json.dumps({"model": model, "max_tokens": 5, "messages": []}).encode(),
                                     headers={"x-api-key": token, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            # The stand-in upstream's answer must arrive whole, not just its status.
            return response.status if b"msg_box_check" in response.read() else "body missing"
    except urllib.error.HTTPError as exc:
        return exc.code

report["claude_ok"] = attempt(lambda: claude(os.environ["ANTHROPIC_API_KEY"], "MODEL_NAME"))
report["claude_wrong_token"] = attempt(lambda: claude("not-the-token", "MODEL_NAME"))
report["claude_other_model"] = attempt(lambda: claude(os.environ["ANTHROPIC_API_KEY"], "claude-other-model"))

def proxy_connect(target):
    with socket.create_connection(("127.0.0.1", 41101), timeout=10) as sock:
        sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        return sock.recv(64).split(b"\r\n", 1)[0].decode()

report["proxy_private"] = attempt(lambda: proxy_connect("10.0.0.1:443"))
report["proxy_loopback"] = attempt(lambda: proxy_connect("127.0.0.1:443"))
report["proxy_port"] = attempt(lambda: proxy_connect("example.com:22"))

def desk(payload):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(30)
        sock.connect(os.environ["OPENMONTAGE_TOOL_DESK"])
        sock.sendall(json.dumps(payload).encode())
        sock.shutdown(socket.SHUT_WR)
        return json.loads(sock.recv(65536))

report["desk_note"] = attempt(lambda: desk({"op": "note", "tool": "box_check", "event": "start", "id": "1"}))
report["desk_local_tool"] = attempt(lambda: desk({"op": "run", "tool": "audio_mixer", "inputs": {}}).get("error"))
# A video's voiceover, asked for on a budget of one cent: the desk must know its price and refuse it.
voice = {"voice_description": "A woman in her thirties from Ohio. Warm, clear voice.", "gender": "woman", "age": "30s",
         "lines": [{"id": "A", "text": "This line is never spoken: the budget of this check is one cent.",
                    "output_path": project + "/assets/audio/A.mp3"}]}
report["desk_voice"] = attempt(lambda: desk({"op": "run", "tool": "voiceover_tts", "inputs": voice}).get("error"))
report["nested_namespace"] = attempt(lambda: subprocess.run(["unshare", "--user", "true"], capture_output=True).returncode)
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall(425, 1, 0)
report["io_uring_errno"] = ctypes.get_errno()
print(json.dumps(report))
'''


# Run in a paid tool's box, as the desk builds one. A video's voice can be the recording that
# comes with its design, cut into the lines there (tools/audio/voiceover_tts.py): the box must
# be able to read and write sound and to time words without the network. A tone has no words,
# and the cutter says exactly that once the transcriber's model has loaded and listened.
CUT_PROBE = r'''
import json, os, subprocess, sys
from array import array
from pathlib import Path
sys.path.insert(0, os.environ["OM_CHECK_REPO"])
from lib.videokit import cut

report = {}
tone = Path("/tmp/cut_check_tone.mp3")
made = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=330:duration=2",
                       "-c:a", "libmp3lame", str(tone)], capture_output=True)
report["tone"] = made.returncode == 0 and tone.is_file()
try:
    cut.cut_lines(tone, [{"id": "A", "text": "A line nobody says.", "output_path": "/tmp/cut_check_A.mp3"}])
    report["listened"] = "cut a tone into a line"
except cut.CutError as exc:
    report["listened"] = str(exc)
except Exception as exc:
    report["listened"] = f"error: {type(exc).__name__}: {exc}"[:300]
try:
    cut._encode(array("h", [0] * cut.RATE), Path("/tmp/cut_check_line.mp3"))
    report["written"] = Path("/tmp/cut_check_line.mp3").stat().st_size > 0
except Exception as exc:
    report["written"] = f"error: {type(exc).__name__}: {exc}"[:300]
print(json.dumps(report))
'''


def stand_in_upstream(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("x-api-key") == STAND_IN_KEY, "the gateway did not add the server's key"
    return httpx.Response(200, json={"id": "msg_box_check", "type": "message", "role": "assistant",
                                     "content": [{"type": "text", "text": "ok"}],
                                     "usage": {"input_tokens": 12, "output_tokens": 3}})


async def run() -> int:
    problem = sandbox_problem()
    if problem:
        print(f"FAIL  this host cannot build agent boxes: {problem}")
        return 1
    work = Path(tempfile.mkdtemp(prefix="box-check-", dir=os.environ.get("OPENMONTAGE_DATA_DIR") or "/data"))
    agent_dir = work / "jobs" / "job_check" / "agent"
    project = work / "projects" / "job_check"
    neighbour = work / "projects" / "job_neighbour"
    for path in (agent_dir, project, neighbour):
        path.mkdir(parents=True)
    (neighbour / "private.txt").write_text("another job's file", encoding="utf-8")
    job_ledger.write_budget(agent_dir, 0.01)  # one cent: no paid call of this check can be bought
    os.environ[MARKER] = "provider-key-outside-the-box"
    probe = PROBE.replace("MARKER_NAME", MARKER).replace("MODEL_NAME", MODEL)
    try:
        async with AgentBox(agent_dir=agent_dir, project_dir=project, model=MODEL, claude_cap_usd=1.0,
                            cli_switches={"OM_CHECK_PROJECT": str(project), "OM_CHECK_REPO": str(REPO_ROOT)},
                            api_key=STAND_IN_KEY, gateway_transport=httpx.MockTransport(stand_in_upstream)) as box:
            (box.tmp / "probe.py").write_text(probe, encoding="utf-8")
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(LAUNCH), str(box.spec_path), sys.executable, "/tmp/probe.py",
                env={**os.environ, **box.session_env()}, cwd=str(REPO_ROOT),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(process.communicate(), timeout=120)
            token = box.session_env()["OM_BOX_CLAUDE_TOKEN"]
            gateway_requests = box.gateway.requests if box.gateway else 0
            # A paid tool's box, built the way the desk builds one (engine/sandbox/desk.py).
            spec = box.box_dir / "cut_check.json"
            spec.write_text(json.dumps({"ro": box.desk.ro_paths, "rw": [[str(project), str(project)], [str(box.tmp), "/tmp"]],
                                        "share_net": True, "env_from_parent": True, "chdir": str(REPO_ROOT)}),
                            encoding="utf-8")
            (box.tmp / "cut_probe.py").write_text(CUT_PROBE, encoding="utf-8")
            cutter = await asyncio.create_subprocess_exec(
                sys.executable, str(LAUNCH), str(spec), sys.executable, "/tmp/cut_probe.py",
                env={**box.desk.provider_env, "OM_CHECK_REPO": str(REPO_ROOT)}, cwd=str(REPO_ROOT),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            cut_out, cut_err = await asyncio.wait_for(cutter.communicate(), timeout=300)
        try:
            report = json.loads(out.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            print("FAIL  the probe did not report:\n" + err.decode("utf-8", "replace")[-2000:])
            return 1
        try:
            cut_report = json.loads(cut_out.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            cut_report = {"listened": "the probe did not report: " + cut_err.decode("utf-8", "replace")[-600:]}
    finally:
        os.environ.pop(MARKER, None)
        shutil.rmtree(work, ignore_errors=True)

    # Every credential this server holds, and the stand-in Anthropic key: none may appear in the box.
    held = {os.environ[name] for name in secret_names(os.environ) if os.environ.get(name)}
    held |= {STAND_IN_KEY, "provider-key-outside-the-box"}
    if os.environ.get("OPENMONTAGE_API_TOKEN"):
        held.add(os.environ["OPENMONTAGE_API_TOKEN"])
    box_values = " ".join(str(value) for value in report["env"].values())
    leaked = [name for name in report["env"] if any(secret and secret in str(report["env"][name]) for secret in held)]
    checks = [
        ("no credential entered the box", not leaked and not any(secret in box_values for secret in held)),
        ("a provider key name shows only the desk placeholder", report["marker"] == "held-by-the-desk"),
        ("Claude's key in the box is the job's token, not the API key",
         report["claude_key"] == token and report["claude_key"] != STAND_IN_KEY),
        ("only the box's own processes are visible",
         len(report["processes"]) <= 8 and not {"uvicorn", "docker-init", "tini"} & set(report["processes"])),
        ("the data folder shows only this job's project", report["projects"] == ["job_check"]),
        ("the repository is read-only", str(report["repo_write"]).startswith("error")),
        ("the project and /tmp are writable", report["project_write"] == "written" and report["tmp_write"] == "written"),
        ("no network of its own", str(report["direct_network"]).startswith("error")),
        ("Claude through the gateway with the job's token", report["claude_ok"] == 200 and gateway_requests == 1),
        ("the gateway refuses another token", report["claude_wrong_token"] == 401),
        ("the gateway refuses a model the job may not use", report["claude_other_model"] == 403),
        ("the web proxy refuses private addresses", " 403 " in f" {report['proxy_private']} "
         and " 403 " in f" {report['proxy_loopback']} "),
        ("the web proxy refuses ports other than 80 and 443", " 403 " in f" {report['proxy_port']} "),
        ("the desk answers the box", report["desk_note"] == {"ok": True}),
        ("the desk runs only paid tools", "runs inside the job" in str(report["desk_local_tool"])),
        ("the desk prices a video's voiceover and holds it to the budget",
         "BUDGET REACHED" in str(report["desk_voice"]) and "voiceover_tts" in str(report["desk_voice"])),
        ("the box cannot create boxes of its own", report["nested_namespace"] not in (0, "0")),
        ("the container's refused system calls stay refused", report["io_uring_errno"] == 1),
        ("a paid tool's box can time words and cut sound (a video's voice as one recording)",
         cut_report.get("tone") is True and cut_report.get("written") is True
         # Either answer means the model loaded and listened: a tone has no words, or none of the line's.
         and str(cut_report.get("listened")).startswith(("no words were heard", "line A was not heard"))),
    ]
    for label, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if leaked:
        print("      leaked:", ", ".join(leaked))
    if not checks[-1][1]:
        print("      the cutter's probe:", json.dumps(cut_report)[:700])
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
