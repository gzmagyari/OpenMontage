"""The agent box's logic that runs on any host: which addresses pass, what the launcher puts in a
box, what a box's environment holds, what the desk decides, and how the tool layer routes calls.
The box itself (bubblewrap) is checked in the container by scripts/agent_box_check.py."""

from __future__ import annotations

import asyncio
import json

import pytest

from engine.sandbox import launch, netcheck
from engine.sandbox import client as desk_client
from engine.sandbox import desk as desk_module
from engine.sandbox.box import AgentBox, provider_env, secret_names
from engine.sandbox.gateway import UsageReader
from lib import job_ledger
from tools.base_tool import BaseTool, ToolResult, ToolRuntime


@pytest.mark.parametrize("address,public", [
    ("1.1.1.1", True), ("8.8.8.8", True), ("2606:4700:4700::1111", True), ("::ffff:8.8.8.8", True),
    ("10.1.2.3", False), ("127.0.0.1", False), ("192.168.65.254", False), ("172.17.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("::1", False), ("fc00::1", False), ("fe80::1", False), ("::ffff:127.0.0.1", False),
    ("64:ff9b::a00:1", False), ("not-an-address", False),
])
def test_only_public_internet_addresses_pass(address, public):
    assert netcheck.is_public_address(address) is public


def test_input_urls_are_found_however_deeply_nested():
    inputs = {"prompt": "see https://shop.example/x", "refs": [{"url": "http://a.example/1.png"}], "n": 3}
    assert netcheck.urls_in(inputs) == ["http://a.example/1.png"]  # a URL inside prose is not an input URL


def test_an_input_url_that_points_inside_is_refused():
    with pytest.raises(netcheck.AddressRefused):
        asyncio.run(netcheck.check_public_urls({"image_url": "http://127.0.0.1:4579/"}))


def _setenv_pairs(argv):
    return {argv[i + 1]: argv[i + 2] for i, arg in enumerate(argv) if arg == "--setenv"}


def test_the_launcher_keeps_only_what_the_spec_names():
    spec = {"ro": ["/usr"], "rw": [["/data/projects/j", "/data/projects/j"]], "env": {"HOME": "/tmp/home"},
            "pass": ["CLAUDE_CODE_ENTRYPOINT"], "rename": {"ANTHROPIC_API_KEY": "OM_BOX_CLAUDE_TOKEN"},
            "chdir": "/app", "init": ["/bin/sh", "init.sh"]}
    environ = {"FAL_KEY": "fal-real", "ANTHROPIC_API_KEY": "anthropic-real", "OM_BOX_CLAUDE_TOKEN": "job-token",
               "CLAUDE_CODE_ENTRYPOINT": "sdk-py", "CLAUDE_CONFIG_DIR": "/elsewhere", "HOME": "/home/app"}
    argv = launch.build_argv(spec, environ, ["claude", "--flag"])
    assert "--clearenv" in argv and "--share-net" not in argv and "--disable-userns" in argv
    assert _setenv_pairs(argv) == {"HOME": "/tmp/home", "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
                                   "ANTHROPIC_API_KEY": "job-token"}
    assert "fal-real" not in argv and "anthropic-real" not in argv
    assert argv[argv.index("--"):] == ["--", "/bin/sh", "init.sh", "claude", "--flag"]


def test_a_provider_box_shares_the_network_and_keeps_the_desks_environment():
    argv = launch.build_argv({"share_net": True, "env_from_parent": True, "chdir": "/app"}, {"FAL_KEY": "x"}, ["py"])
    assert "--share-net" in argv and "--clearenv" not in argv and "--setenv" not in argv


def test_a_box_holds_placeholders_never_credentials(tmp_path, monkeypatch):
    for name, value in {"FAL_KEY": "fal-real", "OPENAI_API_KEY": "openai-real", "OPENMONTAGE_API_TOKEN": "api-real",
                        "ANTHROPIC_API_KEY": "anthropic-real"}.items():
        monkeypatch.setenv(name, value)
    box = AgentBox(agent_dir=tmp_path / "agent", project_dir=tmp_path / "projects" / "job_1", model="claude-haiku-5-5",
                   claude_cap_usd=4.5, cli_switches={"BASH_MAX_TIMEOUT_MS": "1800000"}, api_key="anthropic-real")
    env = box._box_env()
    assert env["FAL_KEY"] == env["OPENAI_API_KEY"] == "held-by-the-desk"
    assert "OPENMONTAGE_API_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert not {"fal-real", "openai-real", "api-real", "anthropic-real"} & set(env.values())
    assert env["BASH_MAX_TIMEOUT_MS"] == "1800000" and env["CLAUDE_CONFIG_DIR"].endswith(".agent_session")
    spec = box._spec()
    assert spec["rename"] == {"ANTHROPIC_API_KEY": "OM_BOX_CLAUDE_TOKEN"}
    assert [str(tmp_path / "projects" / "job_1")] * 2 in spec["rw"]
    assert set(box.session_env()) == {"OM_BOX_CLAUDE_TOKEN"}
    assert json.loads(box.settings_json) == {"skipWebFetchPreflight": True}


def test_a_provider_box_gets_provider_settings_but_nothing_that_drives_the_server():
    env = provider_env({"FAL_KEY": "x", "MINIMAX_REGION": "global", "OPENMONTAGE_API_TOKEN": "t",
                        "CLAUDE_CONFIG_DIR": "c", "OPENMONTAGE_TOOL_DESK": "d", "OPENMONTAGE_JOB_LEDGER_DIR": "l"})
    assert env["FAL_KEY"] == "x" and env["MINIMAX_REGION"] == "global"
    assert not {"OPENMONTAGE_API_TOKEN", "CLAUDE_CONFIG_DIR", "OPENMONTAGE_TOOL_DESK", "OPENMONTAGE_JOB_LEDGER_DIR"} & set(env)
    assert secret_names({"FAL_KEY": "x", "EMPTY_KEY": "", "GPG_KEY": "y", "PATH": "/bin"}) == ["FAL_KEY"]


def test_usage_is_read_from_event_streams_and_json_bodies():
    stream = UsageReader(streaming=True)
    event = {"type": "message_start", "message": {"id": "msg_1", "usage": {"input_tokens": 100}}}
    raw = f"event: message_start\ndata: {json.dumps(event)}\n\n".encode()
    stream.feed(raw[:17])
    stream.feed(raw[17:])
    stream.close()
    assert (stream.message_id, stream.usage) == ("msg_1", {"input_tokens": 100})
    body = UsageReader(streaming=False)
    body.feed(json.dumps({"id": "msg_2", "usage": {"output_tokens": 7}}).encode())
    body.close()
    assert (body.message_id, body.usage) == ("msg_2", {"output_tokens": 7})


# -- the desk ------------------------------------------------------------------------------
class _PaidTool:
    name = "fake_paid"
    provider = "fake"
    runtime = ToolRuntime.API

    def estimate_cost(self, inputs):
        return 0.5


class _LocalTool:
    name = "fake_local"
    provider = "local"
    runtime = ToolRuntime.LOCAL


@pytest.fixture()
def desk(tmp_path, monkeypatch):
    tools = {"fake_paid": _PaidTool(), "fake_local": _LocalTool()}
    monkeypatch.setattr(desk_module, "_tool_registry", lambda: type("R", (), {"get": staticmethod(tools.get)})())
    desk = desk_module.ToolDesk(socket_path=tmp_path / "desk.sock", ledger_dir=tmp_path, project_dir=tmp_path / "p",
                                shared_tmp=tmp_path / "t", specs_dir=tmp_path / "specs", provider_env={},
                                log_path=tmp_path / "desk.log")
    desk.ran = []

    async def provider_box(tool, inputs, cwd):
        desk.ran.append((tool.name, inputs))
        return desk.answer

    desk.answer = {"success": True, "data": {}, "cost_usd": 0.4}
    monkeypatch.setattr(desk, "_in_provider_box", provider_box)
    job_ledger.write_budget(tmp_path, 1.0)
    return desk


def test_the_desk_runs_a_paid_call_and_records_what_it_cost(desk, tmp_path):
    answer = asyncio.run(desk._run({"tool": "fake_paid", "inputs": {"output_path": "a.mp4"}}))
    assert answer["success"] and desk.ran == [("fake_paid", {"output_path": "a.mp4"})]
    state = job_ledger.summarize(job_ledger.read_entries(tmp_path))
    assert state.spent_usd == 0.4 and state.tools_started == ["fake_paid"]


def test_the_desk_holds_a_call_to_the_budget(desk, tmp_path):
    job_ledger.write_budget(tmp_path, 0.3)
    answer = asyncio.run(desk._run({"tool": "fake_paid", "inputs": {}}))
    assert not answer["success"] and "BUDGET REACHED" in answer["error"] and desk.ran == []


def test_the_desk_refuses_local_tools_unknown_tools_and_inside_urls(desk, tmp_path):
    local = asyncio.run(desk._run({"tool": "fake_local", "inputs": {}}))
    unknown = asyncio.run(desk._run({"tool": "no_such_tool", "inputs": {}}))
    inside = asyncio.run(desk._run({"tool": "fake_paid", "inputs": {"image_url": "http://10.0.0.1/x.png"}}))
    assert "runs inside the job" in local["error"] and "no tool named" in unknown["error"]
    assert "not allowed" in inside["error"] and desk.ran == []
    assert not job_ledger.read_entries(tmp_path)


def test_a_paid_call_that_never_finishes_still_counts_at_its_estimate(desk, tmp_path):
    desk.answer = None
    answer = asyncio.run(desk._run({"tool": "fake_paid", "inputs": {"output_path": "a.mp4"}}))
    assert not answer["success"] and "did not finish" in answer["error"]
    assert job_ledger.summarize(job_ledger.read_entries(tmp_path)).spent_usd == 0.5


def test_a_local_tool_is_noted_for_the_stage_at_no_cost(desk, tmp_path):
    desk._note({"event": "start", "id": "n1", "tool": "video_compose", "inputs": {"output_path": "r.mp4"}})
    desk._note({"event": "finish", "id": "n1", "success": True})
    state = job_ledger.summarize(job_ledger.read_entries(tmp_path))
    assert state.tools_started == ["video_compose"] and state.spent_usd == 0.0


# -- the tool layer inside a box ---------------------------------------------------------------
class _RemoteCheck(BaseTool):
    name = "remote_check"
    runtime = ToolRuntime.API

    def execute(self, inputs):
        return ToolResult(success=True, data={"ran": "here"})


class _LocalCheck(BaseTool):
    name = "local_check"
    runtime = ToolRuntime.LOCAL

    def execute(self, inputs):
        return ToolResult(success=True, data={"ran": "here"})


@pytest.fixture()
def desk_requests(monkeypatch):
    sent = []

    def fake_request(payload, timeout=0):
        sent.append(payload)
        return {"success": True, "data": {"ran": "desk"}, "cost_usd": 0.2} if payload["op"] == "run" else {"ok": True}

    monkeypatch.setattr(desk_client, "request", fake_request)
    return sent


def test_inside_a_box_a_paid_tool_runs_at_the_desk(monkeypatch, desk_requests):
    monkeypatch.setenv(desk_client.DESK_ENV, "/run/openmontage/desk.sock")
    result = _RemoteCheck().execute({"output_path": "x.mp4"})
    assert result.data == {"ran": "desk"} and result.cost_usd == 0.2
    assert [(p["op"], p["tool"]) for p in desk_requests] == [("run", "remote_check")]


def test_inside_a_box_a_local_tool_runs_there_and_is_only_noted(monkeypatch, desk_requests):
    monkeypatch.setenv(desk_client.DESK_ENV, "/run/openmontage/desk.sock")
    assert _LocalCheck().execute({}).data == {"ran": "here"}
    assert [(p["op"], p["event"]) for p in desk_requests] == [("note", "start"), ("note", "finish")]


def test_outside_a_box_nothing_changes(monkeypatch, desk_requests):
    monkeypatch.delenv(desk_client.DESK_ENV, raising=False)
    assert _RemoteCheck().execute({}).data == {"ran": "here"} and desk_requests == []


def test_boxes_are_required_unless_switched_off(monkeypatch):
    from engine.config import load_settings

    monkeypatch.delenv("OPENMONTAGE_AGENT_SANDBOX", raising=False)
    assert load_settings().agent_sandbox == "required"
    monkeypatch.setenv("OPENMONTAGE_AGENT_SANDBOX", "Off")
    assert load_settings().agent_sandbox == "off"
