"""The agent box's logic that runs on any host: which addresses pass, what the launcher puts in a
box, what a box's environment holds, what the desk decides, and how the tool layer routes calls.
The box itself (bubblewrap) is checked in the container by scripts/agent_box_check.py."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

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


def test_the_operators_render_switch_reaches_the_box_and_nothing_else_of_its_kind(tmp_path, monkeypatch):
    """KIT_CHROME_TEMP=disk on the container keeps Chrome's temporary files on disk during the
    kit's renders. The box's environment is built from a fixed list, so the switch has to be
    handed in; only its one meaningful value is."""
    def box_env():
        return AgentBox(agent_dir=tmp_path / "agent", project_dir=tmp_path / "projects" / "job_1",
                        model="claude-haiku-5-5", claude_cap_usd=1.0, cli_switches={}, api_key="k")._box_env()

    monkeypatch.delenv("KIT_CHROME_TEMP", raising=False)
    assert "KIT_CHROME_TEMP" not in box_env()
    monkeypatch.setenv("KIT_CHROME_TEMP", "disk")
    assert box_env()["KIT_CHROME_TEMP"] == "disk"
    monkeypatch.setenv("KIT_CHROME_TEMP", "/somewhere/else")
    assert "KIT_CHROME_TEMP" not in box_env()
    # The box's own temp folder is unchanged either way: it is shared with the desk.
    assert box_env()["TMPDIR"] == "/tmp"


def test_a_provider_box_gets_provider_settings_but_nothing_that_drives_the_server():
    env = provider_env({"FAL_KEY": "x", "MINIMAX_REGION": "global", "OPENMONTAGE_API_TOKEN": "t",
                        "CLAUDE_CONFIG_DIR": "c", "OPENMONTAGE_TOOL_DESK": "d", "OPENMONTAGE_JOB_LEDGER_DIR": "l"})
    assert env["FAL_KEY"] == "x" and env["MINIMAX_REGION"] == "global"
    assert not {"OPENMONTAGE_API_TOKEN", "CLAUDE_CONFIG_DIR", "OPENMONTAGE_TOOL_DESK", "OPENMONTAGE_JOB_LEDGER_DIR"} & set(env)
    assert secret_names({"FAL_KEY": "x", "EMPTY_KEY": "", "GPG_KEY": "y", "PATH": "/bin"}) == ["FAL_KEY"]


def test_a_paid_tool_is_told_which_video_it_works_for(tmp_path, monkeypatch):
    """A video's voice is found again by its project (tools/audio/voiceover_tts.py). The desk says
    which project that is; the agent's own box is not told anything new."""
    monkeypatch.setenv("FAL_KEY", "fal-real")
    monkeypatch.setenv("ELEVENLABS_VOICE_API_KEY", "eleven-real")
    monkeypatch.setenv("OPENMONTAGE_INSTANCE_ID", "abc123")
    project = tmp_path / "projects" / "job_1"
    box = AgentBox(agent_dir=tmp_path / "agent", project_dir=project, model="claude-haiku-5-5",
                   claude_cap_usd=1.0, cli_switches={}, api_key="k")
    env = box._provider_env()
    assert env["OPENMONTAGE_PROJECT_DIR"] == str(project)
    assert env["ELEVENLABS_VOICE_API_KEY"] == "eleven-real" and env["OPENMONTAGE_INSTANCE_ID"] == "abc123"
    assert env["FAL_KEY"] == "fal-real" and "OPENMONTAGE_TOOL_DESK" not in env
    inside = box._box_env()
    assert inside["ELEVENLABS_VOICE_API_KEY"] == "held-by-the-desk" and "eleven-real" not in inside.values()
    assert "OPENMONTAGE_PROJECT_DIR" not in inside and "OPENMONTAGE_INSTANCE_ID" not in inside


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


def test_the_desk_keeps_the_providers_request_ids_with_the_cost(desk, tmp_path):
    """Settlement checks each estimate against what the provider really billed, by request id.
    The desk used to drop the ids, so in a real job that check never ran (video 713: every
    line an estimate and no word about it)."""
    desk.answer = {"success": True, "cost_usd": 0.448, "data": {"request_ids": ["01a1c4a1-0b43", ""], "output": "t.mp4"}}
    asyncio.run(desk._run({"tool": "fake_paid", "inputs": {"output_path": "t.mp4"}}))
    item = job_ledger.summarize(job_ledger.read_entries(tmp_path)).items[0]
    assert (item.usd, item.request_ids) == (0.448, ("01a1c4a1-0b43",))
    # A result without ids, or with no data at all, is still recorded, just not checkable.
    desk.answer = {"success": True, "cost_usd": 0.1, "data": None}
    asyncio.run(desk._run({"tool": "fake_paid", "inputs": {"output_path": "u.mp4"}}))
    assert job_ledger.summarize(job_ledger.read_entries(tmp_path)).items[1].request_ids == ()


def test_the_desk_keeps_what_a_tool_left_for_the_operator(desk, tmp_path):
    desk.answer = {"success": True, "cost_usd": 0.03,
                   "data": {"operator_note": "voiceover_tts used the stock voice Bill: no voice slot is free"}}
    asyncio.run(desk._run({"tool": "fake_paid", "inputs": {}}))
    desk.answer = {"success": True, "cost_usd": 0.03, "data": {}}
    asyncio.run(desk._run({"tool": "fake_paid", "inputs": {}}))
    state = job_ledger.summarize(job_ledger.read_entries(tmp_path))
    assert state.notes == ["voiceover_tts used the stock voice Bill: no voice slot is free"]
    assert state.spent_usd == pytest.approx(0.06)


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


class _FilePricedTool:
    """Priced by a file it is given, as a take is by its audio: no file, no price."""
    name = "fake_take"
    provider = "fake"
    runtime = ToolRuntime.API

    def estimate_cost(self, inputs):
        from tools.provider_pricing import PriceQuoteRequired

        paths = [inputs.get("audio_path"), *(inputs.get("image_paths") or [])]
        files = [Path(p) for p in paths if p]
        if not files or not all(f.is_file() for f in files):
            raise PriceQuoteRequired("nothing to price")
        return round(sum(len(f.read_bytes()) for f in files) / 1000, 3)


@pytest.fixture()
def take_desk(desk, tmp_path, monkeypatch):
    tools = {"fake_take": _FilePricedTool()}
    monkeypatch.setattr(desk_module, "_tool_registry", lambda: type("R", (), {"get": staticmethod(tools.get)})())
    project = tmp_path / "p"
    (project / "assets" / "audio").mkdir(parents=True)
    (project / "work").mkdir()
    (project / "assets" / "audio" / "voice_A.wav").write_bytes(b"a" * 400)
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "line.wav").write_bytes(b"b" * 300)
    (tmp_path / "t" / "ref.png").write_bytes(b"c" * 100)
    job_ledger.write_budget(tmp_path, 5.0)
    return desk


def test_the_desk_quotes_a_call_where_the_job_keeps_its_files(take_desk, tmp_path):
    """The quote is made in the API process, the tool runs in the job's folder. `kit voice` lists
    a take's audio relative to the project; a batch passed it as listed, and the desk looked for
    it in the repository: four takes of video 716 were refused with "no price for this call"."""
    desk, project = take_desk, tmp_path / "p"
    given = {"audio_path": "assets/audio/voice_A.wav", "output_path": "assets/video/take_A.mp4"}
    answer = asyncio.run(desk._run({"tool": "fake_take", "inputs": given, "cwd": str(project)}))
    assert answer["success"], answer
    assert desk.ran == [("fake_take", given)]  # the tool runs with the paths as the job gave them
    started = job_ledger.read_entries(tmp_path)[0]
    assert started["event"] == "start" and started["estimate_usd"] == 0.4
    # The same from a subfolder of the project, and with the absolute path.
    for inputs, cwd in (({"audio_path": "../assets/audio/voice_A.wav"}, str(project / "work")),
                        ({"audio_path": str(project / "assets" / "audio" / "voice_A.wav")}, "")):
        assert asyncio.run(desk._run({"tool": "fake_take", "inputs": inputs, "cwd": cwd}))["success"], inputs


def test_the_desk_reads_the_boxs_tmp_where_it_really_is(take_desk, tmp_path):
    # Inside the box /tmp is the job's own temp folder; for the desk it is another folder.
    desk = take_desk
    inputs = {"audio_path": "/tmp/line.wav", "image_paths": ["/tmp/ref.png"]}
    answer = asyncio.run(desk._run({"tool": "fake_take", "inputs": inputs, "cwd": "/tmp"}))
    assert answer["success"], answer
    assert job_ledger.read_entries(tmp_path)[0]["estimate_usd"] == 0.4  # 300 + 100 bytes
    seen = desk._as_seen_from_here({"audio_path": "line.wav", "image_url": "https://example.com/a.png",
                                    "prompt": "a/b", "image_paths": ["/tmp/ref.png"], "duration": 5}, "/tmp")
    assert seen == {"audio_path": str(tmp_path / "t" / "line.wav"), "image_url": "https://example.com/a.png",
                    "prompt": "a/b", "image_paths": [str(tmp_path / "t" / "ref.png")], "duration": 5}


def test_a_file_that_is_not_there_is_still_refused(take_desk, tmp_path):
    desk, project = take_desk, tmp_path / "p"
    for inputs, cwd in (({"audio_path": "assets/audio/voice_Z.wav"}, str(project)),
                        ({"audio_path": "assets/audio/voice_A.wav"}, "/etc")):  # not a folder of the job
        answer = asyncio.run(desk._run({"tool": "fake_take", "inputs": inputs, "cwd": cwd}))
        assert not answer["success"] and "NO PRICE FOR THIS CALL" in answer["error"], inputs
    assert desk.ran == []


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
