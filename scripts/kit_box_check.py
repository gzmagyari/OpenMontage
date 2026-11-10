#!/usr/bin/env python3
"""Check the video kit inside a job's agent box on this host. Run it after an image rebuild.

No model and no paid tool: a real box is built the way a job builds one (engine/sandbox/box.py),
the kit's workspace is staged the way the runner stages it, and INSIDE the box a small video is
made end to end from synthetic media, in the order a job works: a draft check and a draft
sheet while nothing has been generated yet (a shot, a scene's backdrop and the music are all
still missing), then voice timing, soundtrack, check, sheet, render. It passes when every kit
command works under the box's rules (read-only repository, no network, only the project and
/tmp writable) and the export is what the kit promises.

    docker exec -w /app openmontage-api python scripts/kit_box_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402

from engine.sandbox.box import LAUNCH, AgentBox, sandbox_problem  # noqa: E402
from lib.videokit.stage import stage_workspace  # noqa: E402

MODEL = "claude-haiku-5-5"
STAND_IN_KEY = "stand-in-anthropic-key-outside-the-box"

EDIT = {
    "canvas": {"size": "540x960", "fps": 30, "safe": "tiktok"},
    "voice": {"lines": [{"id": "A", "text": "A tone stands in for the voice."}]},
    "music": {"file": "assets/audio/music.wav"},
    "main": [
        {"video": "assets/video/clip.mp4", "until": {"word": "stands"}},
        {"scene": "KineticText", "props": {"text": "Made in\nthe box", "emphasis": "box", "backdrop": {"video": "assets/video/back.mp4", "dim": 0.5}},
         "until": {"voice": "end"}, "enter": "slide-up"},
        {"video": "assets/video/clip.mp4", "start": 1, "for": 1},
    ],
    "overlays": [{"tag": "hello", "from": 0.3, "for": 1.2}],
}

# What the director would run, in the order it runs it. The draft comes while the paid batch
# would still be generating: the kit has to show what is missing without anyone making a
# stand-in file, and must put nothing where the real files belong.
STEPS = [
    ("restyle", "sed -i 's/#FFD400/#35E0A1/' composition/src/look/tokens.ts"),
    ("check --draft", "kit check --draft > /tmp/draft.txt; code=$?; cat /tmp/draft.txt;"
                      " [ $code -eq 0 ] && grep -q 'Still generating (3)' /tmp/draft.txt"),
    ("sheet --draft", "kit sheet --draft && test -f work/kit/sheet.jpg && test ! -e assets && rm work/kit/sheet.jpg"),
    ("media", "mkdir -p assets/audio assets/video"
              " && ffmpeg -y -v error -f lavfi -i sine=frequency=330:duration=2.5 assets/audio/A.wav"
              " && ffmpeg -y -v error -f lavfi -i sine=frequency=220:duration=8 assets/audio/music.wav"
              " && ffmpeg -y -v error -f lavfi -i testsrc2=size=540x960:rate=30:duration=4 -pix_fmt yuv420p assets/video/clip.mp4"
              " && cp assets/video/clip.mp4 assets/video/back.mp4"),
    ("voice", "kit voice"),
    ("mix", "kit mix"),
    ("check", "kit check"),
    ("sheet", "kit sheet"),
    ("render", "kit render --name boxcheck"),
]

RUNNER = r'''
import json, os, subprocess, sys, time
os.chdir(os.environ["OM_CHECK_PROJECT"])
report = []
for name, command in json.loads(os.environ["OM_CHECK_STEPS"]):
    started = time.time()
    done = subprocess.run(command, shell=True, capture_output=True, text=True)
    report.append({"step": name, "code": done.returncode, "seconds": round(time.time() - started, 1),
                   "tail": (done.stdout + done.stderr)[-700:]})
    if done.returncode != 0:
        break
print(json.dumps(report))
'''


def stand_in_upstream(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "msg_kit_check", "type": "message", "role": "assistant",
                                     "content": [{"type": "text", "text": "ok"}],
                                     "usage": {"input_tokens": 1, "output_tokens": 1}})


async def run() -> int:
    problem = sandbox_problem()
    if problem:
        print(f"FAIL  this host cannot build agent boxes: {problem}")
        return 1
    work = Path(tempfile.mkdtemp(prefix="kit-check-", dir=os.environ.get("OPENMONTAGE_DATA_DIR") or "/data"))
    agent_dir = work / "jobs" / "job_check" / "agent"
    project = work / "projects" / "job_check"
    for path in (agent_dir, project):
        path.mkdir(parents=True)
    try:
        staged = stage_workspace(project)  # outside the box, as the runner does at job start
        (project / "composition" / "edit.json").write_text(json.dumps(EDIT), encoding="utf-8")
        async with AgentBox(agent_dir=agent_dir, project_dir=project, model=MODEL, claude_cap_usd=1.0,
                            cli_switches={"OM_CHECK_PROJECT": str(project), "OM_CHECK_STEPS": json.dumps(STEPS)},
                            api_key=STAND_IN_KEY, gateway_transport=httpx.MockTransport(stand_in_upstream)) as box:
            (box.tmp / "kit_runner.py").write_text(RUNNER, encoding="utf-8")
            started = time.time()
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(LAUNCH), str(box.spec_path), sys.executable, "/tmp/kit_runner.py",
                env={**os.environ, **box.session_env()}, cwd=str(REPO_ROOT),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(process.communicate(), timeout=600)
            total = time.time() - started
        try:
            report = json.loads(out.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            print("FAIL  the kit run did not report:\n" + err.decode("utf-8", "replace")[-2000:])
            return 1

        ran = {step["step"]: step for step in report}
        export = project / "exports" / "boxcheck.mp4"
        probe: dict = {}
        if export.is_file():
            probe = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,width,height,color_space:format=duration",
                 "-of", "json", str(export)], capture_output=True, text=True).stdout or "{}")
        video = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
        public = project / "work" / "kit" / "public" / "assets" / "video" / "clip.mp4"
        checks = [
            ("the runner's staging gave the box a workspace", "composition/edit.json" in staged and "composition/src/look" in staged),
            *[(f"`{name}` works in the box", name in ran and ran[name]["code"] == 0) for name, _ in STEPS],
            ("footage was staged without copying it (a hard link inside the project)",
             public.is_file() and os.stat(public).st_nlink >= 2),
            ("the check sheet exists", (project / "work" / "kit" / "sheet.jpg").is_file()),
            ("the export is H.264, the edit's size, BT.709",
             (video.get("codec_name"), video.get("width"), video.get("height"), video.get("color_space")) == ("h264", 540, 960, "bt709")),
            ("the export has sound", any(s.get("codec_type") == "audio" for s in probe.get("streams", []))),
        ]
        for label, ok in checks:
            print(f"{'PASS' if ok else 'FAIL'}  {label}")
        for step in report:
            print(f"      {step['step']:<14} {step['seconds']:>5.1f} s" + ("" if step["code"] == 0 else f"   exit {step['code']}\n{step['tail']}"))
        print(f"      in the box, all steps: {total:.1f} s")
        return 0 if all(ok for _, ok in checks) else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
