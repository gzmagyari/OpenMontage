"""The final draw of a kit video on Remotion Lambda instead of on this machine.

`kit render` calls this by itself when the server is set to draw in the cloud (KIT_RENDER=cloud);
a director never calls it. A local draw holds several gigabytes of memory for a minute, so how
many videos a server can finish at once is set by its memory. Drawn on Lambda, a video costs
this machine an upload and a download, and the videos stop competing with each other.

It needs the network and the AWS credentials, which a job's agent box has neither of: like every
paid tool it is run by the paid-tools desk, in a provider box that sees only the job's project.
The work itself is `remotion-composer/kit/cli/cloud.mjs`. A failure here is not the end of the
video: `kit render` then draws it locally as before.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CLOUD_SCRIPT = REPO_ROOT / "remotion-composer" / "kit" / "cli" / "cloud.mjs"
# What one draw is held to in the job's budget before it runs. Measured draws of 13 to 29 second
# videos cost $0.015 to $0.032 in function time (2026-11-16); the real figure is recorded after.
ESTIMATE_USD_PER_SECOND = 0.002
ESTIMATE_FLOOR_USD = 0.03
TIMEOUT_SECONDS = 15 * 60


class CloudRender(BaseTool):
    name = "cloud_render"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "render_offload"
    provider = "aws-lambda"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    install_instructions = (
        "Deploy a Remotion Lambda function of the same Remotion version, then set "
        "REMOTION_AWS_ACCESS_KEY_ID, REMOTION_AWS_SECRET_ACCESS_KEY and REMOTION_AWS_REGION on the "
        "server, and KIT_RENDER=cloud to make `kit render` use it (docs/API.md, \"Drawing in the cloud\")."
    )
    capabilities = ["render_offload"]
    best_for = ["used by `kit render` itself when the server draws in the cloud"]
    not_good_for = ["calling directly: run `kit render`, which stages the project first"]

    input_schema = {
        "type": "object",
        "required": ["project_path"],
        "properties": {
            "project_path": {"type": "string", "description": "The job's project directory, staged by `kit render`."},
            "name": {"type": "string", "description": "The export's name; the draw lands in renders/<name>_raw.mp4."},
            "seconds": {"type": "number", "description": "The video's length, for the price estimate."},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=500, network_required=True)
    retry_policy = RetryPolicy(max_retries=0)
    side_effects = ["uploads the staged composition and its media to the server's own S3 bucket and removes them",
                    "writes renders/<name>_raw.mp4"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if os.environ.get("REMOTION_AWS_SECRET_ACCESS_KEY") else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, object]) -> float:
        try:
            seconds = float(inputs.get("seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        return round(max(ESTIMATE_FLOOR_USD, seconds * ESTIMATE_USD_PER_SECOND), 4)

    def estimate_runtime(self, inputs: dict[str, object]) -> float:
        return 60.0

    def execute(self, inputs: dict[str, object]) -> ToolResult:
        started = time.time()
        project = Path(str(inputs.get("project_path") or "")).resolve()
        # In a job the provider box is told which video it works for; nothing else may be drawn.
        own = os.environ.get("OPENMONTAGE_PROJECT_DIR")
        if own and project != Path(own).resolve():
            return ToolResult(success=False, error="cloud_render draws the job's own project only")
        if not (project / "work" / "kit" / "cloud_props.json").is_file():
            return ToolResult(success=False, error="the project is not staged for a cloud draw: run `kit render`, "
                                                    "which stages it and calls this by itself")
        name = str(inputs.get("name") or "video")
        try:
            done = subprocess.run(["node", str(CLOUD_SCRIPT), "--project", str(project), "--name", name],
                                  capture_output=True, text=True, timeout=TIMEOUT_SECONDS, cwd=str(project))
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error=f"the cloud draw did not finish in {TIMEOUT_SECONDS // 60} minutes",
                              cost_usd=self.estimate_cost(inputs))
        except OSError as exc:
            return ToolResult(success=False, error=f"the cloud draw could not start: {exc}")
        facts: dict = {}
        for line in reversed((done.stdout or "").strip().splitlines()):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                facts = parsed
                break
        cost = float(facts.get("cost_usd") or 0.0)
        if not facts.get("ok"):
            reason = str(facts.get("error") or (done.stderr or "").strip()[-600:] or f"exit {done.returncode}")
            return ToolResult(success=False, error=f"the cloud draw failed: {reason}", cost_usd=cost, data=facts)
        return ToolResult(success=True, data=facts, artifacts=[str(project / str(facts.get("file")))], cost_usd=cost,
                          duration_seconds=round(time.time() - started, 2))
