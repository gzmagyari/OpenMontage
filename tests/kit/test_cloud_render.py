"""The final draw in the cloud (tools/video/cloud_render.py, lib/videokit/cloud.py): what it costs
in the budget, what it refuses, what it answers, and that the box is told of the switch."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from engine.sandbox.box import AgentBox, secret_names
from lib.videokit import cloud
from tools.base_tool import ToolRuntime, ToolStatus
from tools.video import cloud_render
from tools.video.cloud_render import CloudRender

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_job(monkeypatch):
    # Outside a job: no desk to forward to and no ledger, so the tool runs here.
    for name in ("OPENMONTAGE_TOOL_DESK", "OPENMONTAGE_JOB_LEDGER_DIR", "OPENMONTAGE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)


def staged(tmp_path: Path) -> Path:
    project = tmp_path / "job_1"
    (project / "work" / "kit" / "public").mkdir(parents=True)
    (project / "work" / "kit" / "cloud_props.json").write_text("{}", encoding="utf-8")
    return project


def test_it_is_a_desk_tool_with_a_price_before_it_runs(monkeypatch):
    tool = CloudRender()
    # It needs the network and the credentials, so in a job only the paid-tools desk runs it.
    assert tool.runtime is ToolRuntime.API and tool.name == "cloud_render"
    # A paid call without a price is refused in a job: every draw has one, by the video's length.
    assert tool.estimate_cost({}) == 0.03 == tool.estimate_cost({"seconds": "soon"})
    assert tool.estimate_cost({"seconds": 10}) == 0.03 and tool.estimate_cost({"seconds": 60}) == 0.12
    monkeypatch.delenv("REMOTION_AWS_SECRET_ACCESS_KEY", raising=False)
    assert tool.get_status() is ToolStatus.UNAVAILABLE
    # Inside a job's box the key's name holds a placeholder: the tool still counts as set up there.
    monkeypatch.setenv("REMOTION_AWS_SECRET_ACCESS_KEY", "held-by-the-desk")
    assert tool.get_status() is ToolStatus.AVAILABLE
    assert "REMOTION_AWS_SECRET_ACCESS_KEY" in secret_names({"REMOTION_AWS_SECRET_ACCESS_KEY": "x"})


def test_it_draws_only_a_staged_project_and_only_the_jobs_own(tmp_path, monkeypatch):
    ran = []
    monkeypatch.setattr(cloud_render.subprocess, "run", lambda *a, **k: ran.append(a))
    bare = tmp_path / "job_0"
    bare.mkdir()
    refused = CloudRender().execute({"project_path": str(bare)})
    assert not refused.success and "not staged" in refused.error
    project = staged(tmp_path)
    monkeypatch.setenv("OPENMONTAGE_PROJECT_DIR", str(tmp_path / "job_2"))
    other = CloudRender().execute({"project_path": str(project)})
    assert not other.success and "own project only" in other.error
    assert ran == []


def test_a_finished_draw_reports_its_real_cost_and_its_file(tmp_path, monkeypatch):
    project = staged(tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        facts = {"ok": True, "region": "us-east-1", "upload_seconds": 14.5, "render_seconds": 31.4, "functions": 38,
                 "cost_usd": 0.0228, "download_seconds": 2.5, "file": "renders/video_raw.mp4"}
        return subprocess.CompletedProcess(argv, 0, stdout="bundling...\n" + json.dumps(facts) + "\n", stderr="")
    monkeypatch.setattr(cloud_render.subprocess, "run", run)
    result = CloudRender().execute({"project_path": str(project), "name": "video", "seconds": 24.9})
    assert result.success and result.cost_usd == 0.0228 and result.data["functions"] == 38
    assert result.artifacts == [str(project / "renders/video_raw.mp4")]
    argv, kwargs = calls[0]
    assert argv[0] == "node" and argv[1] == str(REPO / "remotion-composer/kit/cli/cloud.mjs")
    assert argv[2:] == ["--project", str(project), "--name", "video"] and kwargs["timeout"] == 15 * 60


def test_a_failed_draw_says_why_and_still_reports_what_it_cost(tmp_path, monkeypatch):
    project = staged(tmp_path)

    def failed(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout=json.dumps({"ok": False, "cost_usd": 0.004,
                                                                       "error": "the draw failed on Lambda: boom"}), stderr="")
    monkeypatch.setattr(cloud_render.subprocess, "run", failed)
    result = CloudRender().execute({"project_path": str(project)})
    assert not result.success and "boom" in result.error and result.cost_usd == 0.004

    def silent(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="Error: Cannot find module\n")
    monkeypatch.setattr(cloud_render.subprocess, "run", silent)
    result = CloudRender().execute({"project_path": str(project)})
    assert not result.success and "Cannot find module" in result.error and result.cost_usd == 0.0

    def stuck(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    monkeypatch.setattr(cloud_render.subprocess, "run", stuck)
    result = CloudRender().execute({"project_path": str(project), "seconds": 30})
    # A draw that never answered may still have been billed: it counts at its estimate.
    assert not result.success and "did not finish" in result.error and result.cost_usd == 0.06


def test_the_kits_way_out_prints_one_line_and_tells_success_by_its_exit(tmp_path, monkeypatch, capsys):
    project = staged(tmp_path)

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"ok": True, "cost_usd": 0.02, "file": "renders/a_raw.mp4"}), stderr="")
    monkeypatch.setattr(cloud_render.subprocess, "run", run)
    assert cloud.main([str(project), "a", "24.9"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ok"] is True

    def run_failed(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout=json.dumps({"ok": False, "error": "no function"}), stderr="")
    monkeypatch.setattr(cloud_render.subprocess, "run", run_failed)
    assert cloud.main([str(project), "a"]) == 1
    said = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert said["ok"] is False and "no function" in said["error"]
    assert cloud.main([]) == 2


def test_the_cloud_switch_reaches_the_box_and_the_credentials_do_not(tmp_path, monkeypatch):
    def box_env():
        return AgentBox(agent_dir=tmp_path / "agent", project_dir=tmp_path / "projects" / "job_1",
                        model="claude-haiku-5-5", claude_cap_usd=1.0, cli_switches={}, api_key="k")._box_env()

    monkeypatch.delenv("KIT_RENDER", raising=False)
    assert "KIT_RENDER" not in box_env()
    monkeypatch.setenv("KIT_RENDER", "cloud")
    assert box_env()["KIT_RENDER"] == "cloud"
    monkeypatch.setenv("KIT_RENDER", "elsewhere")
    assert "KIT_RENDER" not in box_env()
    monkeypatch.setenv("REMOTION_AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("REMOTION_AWS_SECRET_ACCESS_KEY", "the-real-secret")
    env = box_env()
    assert env["REMOTION_AWS_SECRET_ACCESS_KEY"] == "held-by-the-desk" and "REMOTION_AWS_ACCESS_KEY_ID" not in env
    assert "the-real-secret" not in json.dumps(env)


def test_the_kit_asks_the_cloud_first_and_draws_locally_when_it_cannot():
    kit = (REPO / "remotion-composer/kit/cli/kit.mjs").read_text(encoding="utf-8")
    assert "if (process.env.KIT_RENDER === 'cloud') {" in kit and "cloud = cloudDraw(resolved, prepared.edit, name, raw);" in kit
    # No finished file is "not this time": the local draw follows, with the browser opened only then.
    assert "if (facts?.ok && fs.existsSync(raw)) return facts;" in kit
    local = kit.index("if (!stage) {")
    assert kit.index("cloud = cloudDraw(") < local < kit.index("stage = await openStage(resolved, prepared.edit);", local) < kit.index("await renderMedia({")
    assert "renderer, ...(cloud ? {cloud} : {})," in kit
    script = (REPO / "remotion-composer/kit/cli/cloud.mjs").read_text(encoding="utf-8")
    # What was uploaded is removed whatever happened, and a draw's file expires by itself.
    assert script.index("} finally {") < script.index("await lambda.deleteSite({bucketName, siteName, region});")
    assert "deleteAfter: '1-day'" in script and "privacy: 'private'" in script
    package = json.loads((REPO / "remotion-composer/package.json").read_text(encoding="utf-8"))
    assert package["dependencies"]["@remotion/lambda"] == "4.0.484"
