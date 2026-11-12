"""POST /v1/videos end to end with the REAL agent runners and a fake SDK stream.

The fake replaces engine.agent.session.sdk_query with an async generator that
yields genuine claude_agent_sdk dataclasses and performs the side effect a real
run has (writing the export + report), so everything from the HTTP layer through
brief building, journaling, stage mapping, cost lines and file collection runs
for real - with zero paid calls.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
import pytest_asyncio
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import engine.agent.session as agent_session
from api.main import create_app
from tests.api.helpers import POST, record_tool_call, write_posting

pytestmark = pytest.mark.asyncio


def _project_dir(prompt: str) -> str:
    match = re.search(r"All project work goes in: (.+)", prompt)
    assert match, "the brief must name the project directory"
    return match.group(1).strip()


class FakeSDK:
    """Configurable fake for engine.agent.session.sdk_query."""

    def __init__(self):
        self.calls: list[dict] = []
        self.api_key_source = "ANTHROPIC_API_KEY"
        self.subtype = "success"
        self.delay = 0.0
        self.hang = False
        self.hang_from_call = None
        self.export_from_call = 1
        self.request_ids: dict[str, list[str]] = {}  # tool -> what its provider call returned
        self.write_post = True    # the run leaves its post text beside the export
        self.write_cover = True   # ...and its cover picture
        self.cover_at = 1.6       # the second `kit cover` recorded (None: no record)
        self.cover_of = None      # the export it recorded (None: the one this call wrote)
        self.answer_post = True   # the follow-up that asks for a missing post text writes it
        self.notes: dict[str, str] = {}  # tool -> what it left for the operator

    def install(self, monkeypatch):
        fake = self

        async def fake_query(*, prompt, options=None, **_):
            fake.calls.append({"prompt": prompt, "options": options})
            session_id = f"sess-{len(fake.calls)}"
            yield SystemMessage(subtype="init", data={
                "session_id": session_id, "apiKeySource": fake.api_key_source,
                "model": options.model, "tools": ["Bash", "Read"]})
            if "the text the client posts with it is missing" in prompt:
                # The one follow-up of a finished video: it writes the post text and nothing else.
                if fake.answer_post:
                    write_posting(agent_session.Path(_project_dir(fake.calls[0]["prompt"])), cover=False)
                yield ResultMessage(subtype="success", duration_ms=100, duration_api_ms=90, is_error=False,
                                    num_turns=8, session_id=options.resume or session_id, total_cost_usd=0.24,
                                    result="done")
                return
            if fake.hang or (fake.hang_from_call and len(fake.calls) >= fake.hang_from_call):
                await asyncio.sleep(3600)
            yield AssistantMessage(
                content=[TextBlock(text="Reading the brief and planning the production."),
                         ToolUseBlock(id="t1", name="Bash",
                                      input={"command": "python -c 'fal_elevenlabs_tts'"})],
                model=options.model)
            record_tool_call(options, "fal_elevenlabs_tts", 0.02, model="eleven-v3", output="/p/vo.mp3",
                             request_ids=fake.request_ids.get("fal_elevenlabs_tts"),
                             note=fake.notes.get("fal_elevenlabs_tts"))
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="tts ok", is_error=False)])
            yield AssistantMessage(
                content=[ToolUseBlock(id="t2", name="Bash",
                                      input={"command": "python -c 'fal_avatar h3max-acting'"})],
                model=options.model)
            record_tool_call(options, "fal_avatar", 0.88, model="h3max-acting", output="/p/take1.mp4",
                             request_ids=fake.request_ids.get("fal_avatar"))
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="take ok", is_error=False)])
            if fake.delay:
                await asyncio.sleep(fake.delay)
            if fake.subtype == "success" and len(fake.calls) >= fake.export_from_call:
                # The revise prompt names the project dir too; each call uses its own.
                project = _project_dir(prompt if "goes in:" in prompt else fake.calls[0]["prompt"])
                exports = agent_session.Path(project) / "exports"
                exports.mkdir(parents=True, exist_ok=True)
                name = "video_rev2.mp4" if options.resume else "video.mp4"
                (exports / name).write_bytes(b"fake-mp4-bytes")
                (agent_session.Path(project) / "RUN_REPORT.md").write_text("# report", encoding="utf-8")
                artifacts = agent_session.Path(project) / "artifacts"
                artifacts.mkdir(parents=True, exist_ok=True)
                (artifacts / "cost_log.json").write_text(
                    json.dumps([{"item": "fal_avatar take1", "usd": 0.88},
                                {"item": "tts", "usd": 0.02}]), encoding="utf-8")
                write_posting(agent_session.Path(project), post=fake.write_post, cover=fake.write_cover,
                              cover_at=fake.cover_at, cover_of=fake.cover_of or name)
                yield AssistantMessage(content=[TextBlock(text="Exported the final video.")],
                                       model=options.model)
            yield ResultMessage(subtype=fake.subtype, duration_ms=1234, duration_api_ms=1000,
                                is_error=fake.subtype != "success", num_turns=7,
                                session_id=session_id, total_cost_usd=0.21,
                                result="done" if fake.subtype == "success" else "budget exceeded")

        monkeypatch.setattr(agent_session, "sdk_query", fake_query)
        return fake


@pytest.fixture()
def fake_sdk(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("FAL_KEY", "test-fal-key")
    return FakeSDK().install(monkeypatch)


@pytest_asyncio.fixture()
async def agent_client(settings, fake_sdk):
    import dataclasses

    import httpx

    cfg = dataclasses.replace(settings, max_active_jobs=12, max_queued_jobs=20,
                              agent_wall_time_minutes=0.05)  # 3 s: fits the hang test, clears the rest
    app = create_app(settings=cfg)  # real build_runners(): the agent runners
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Authorization": "Bearer test-token"}) as http:
            http.app = app  # type: ignore[attr-defined]
            yield http


async def _wait_terminal(client, job_id, timeout=10.0):
    async with asyncio.timeout(timeout):
        while True:
            data = (await client.get(f"/v1/jobs/{job_id}")).json()
            if data["status"] in ("completed", "failed", "cancelled"):
                return data
            await asyncio.sleep(0.02)


async def test_video_job_end_to_end(agent_client, fake_sdk):
    response = await agent_client.post("/v1/videos", json={
        "prompt": "A 15s TikTok ad for example.com with a presenter",
        "options": {"duration_seconds": 15, "max_budget_usd": 5},
    })
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]

    data = await _wait_terminal(agent_client, job_id)
    assert data["status"] == "completed", data.get("error")
    assert data["result"]["video"]["name"] == "video.mp4"
    assert data["result"]["session_id"] == "sess-1"
    assert data["result"]["agent"]["cost_usd"] == 0.21

    # Cost lines: the claude spend plus what the tools recorded.
    items = {line["item"]: line["usd"] for line in data["cost"]["lines"]}
    assert items == {"claude agent": 0.21, "fal_avatar h3max-acting (take1.mp4)": 0.88,
                     "fal_elevenlabs_tts eleven-v3 (vo.mp3)": 0.02}
    assert data["cost"]["spent_usd"] == pytest.approx(0.21 + 0.88 + 0.02)
    assert data["warnings"] == []

    # The chat stream is in the poll response and pages correctly.
    transcript = data["transcript"]
    kinds = [event["kind"] for event in transcript["events"]]
    assert kinds[0] == "session"
    assert "assistant" in kinds and "tool" in kinds and "tool_result" in kinds and kinds[-1] == "result"
    page = (await agent_client.get(
        f"/v1/jobs/{job_id}?transcript_offset={transcript['next_offset']}")).json()["transcript"]
    assert page["events"] == [] and page["next_offset"] == transcript["next_offset"]

    # The export and the report download as job files.
    files = (await agent_client.get(f"/v1/jobs/{job_id}/files")).json()["files"]
    names = {entry["name"] for entry in files}
    assert {"video.mp4", "RUN_REPORT.md", "journal.jsonl", "transcript.jsonl"} <= names
    video = await agent_client.get(f"/v1/jobs/{job_id}/files/video.mp4")
    assert video.status_code == 200 and video.content == b"fake-mp4-bytes"

    # The brief carried the request and the locked rules.
    brief = fake_sdk.calls[0]["prompt"]
    assert "example.com" in brief and "h3max-acting" in brief and "$5.00" in brief
    assert '`openai_image` with `model: "gpt-image-2.6-flare"` and `quality: "high"`' in brief
    assert "no other real brand names, logos or labels" in brief
    options = fake_sdk.calls[0]["options"]
    assert options.permission_mode == "bypassPermissions"
    assert options.setting_sources == ["project"]
    assert options.env.get("ANTHROPIC_API_KEY") == "test-anthropic-key"
    assert options.env.get("FAL_KEY") == "test-fal-key"

    # One continuous turn: a slow provider call waits in the foreground, nothing moves to the
    # background, and the tools that end a turn with work still running are not offered.
    assert options.tools == agent_session.AGENT_TOOLS
    # Task is offered on purpose since delegation: subagents run in the foreground, inside the turn.
    assert not {"ScheduleWakeup", "Monitor", "CronCreate", "Workflow"} & set(options.tools)
    assert options.env.get("BASH_DEFAULT_TIMEOUT_MS") == str(20 * 60 * 1000)
    assert options.env.get("BASH_MAX_TIMEOUT_MS") == str(30 * 60 * 1000)
    assert options.env.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS") == "1"

    # The session lives in the project on the data volume, not in the container's home.
    session_home = agent_session.Path(options.env["CLAUDE_CONFIG_DIR"])
    assert session_home.is_dir() and session_home.parent.name == job_id


async def test_settlement_bills_what_fal_billed_and_names_the_charges_it_could_not_check(
        agent_client, fake_sdk, monkeypatch):
    """Video 713 settled with every line an estimate and not a word about it: the billing key
    was set, but no request id had reached the ledger, so the check was skipped in silence."""
    from lib import fal_billing

    asked: list[list[str]] = []

    def billed(request_ids, since_iso, key, timeout=20.0):
        asked.append(list(request_ids))
        assert key == "the-billing-key"
        return {"rid-take": 0.93}

    monkeypatch.setenv(fal_billing.ADMIN_KEY_ENV, "the-billing-key")
    monkeypatch.setattr(fal_billing, "fetch_events", billed)
    fake_sdk.request_ids = {"fal_avatar": ["rid-take"]}  # the voice line brings none

    response = await agent_client.post("/v1/videos", json={
        "prompt": "A 15s TikTok ad for example.com with a presenter",
        "options": {"duration_seconds": 15, "max_budget_usd": 5},
    })
    assert response.status_code == 202, response.text
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "completed", data.get("error")

    # Only our own request ids are asked about, and the take is billed at fal's figure.
    assert asked == [["rid-take"]]
    lines = {line["item"]: (line["usd"], line["estimated"]) for line in data["cost"]["lines"]}
    assert lines["fal_avatar h3max-acting (take1.mp4)"] == (0.93, False)
    assert lines["fal_elevenlabs_tts eleven-v3 (vo.mp3)"] == (0.02, True)
    assert data["cost"]["spent_usd"] == pytest.approx(0.21 + 0.93 + 0.02)
    # The charge nobody could check is named, once.
    unchecked = [warning for warning in data["warnings"] if "without a request id" in warning]
    assert len(unchecked) == 1
    assert "1 (fal_elevenlabs_tts eleven-v3 (vo.mp3))" in unchecked[0]


async def test_a_job_with_no_request_ids_at_all_still_says_so(agent_client, fake_sdk, monkeypatch):
    from lib import fal_billing

    def never(*_args, **_kwargs):
        raise AssertionError("there is nothing to ask fal about")

    monkeypatch.setenv(fal_billing.ADMIN_KEY_ENV, "the-billing-key")
    monkeypatch.setattr(fal_billing, "fetch_events", never)

    response = await agent_client.post("/v1/videos", json={
        "prompt": "A 15s TikTok ad for example.com with a presenter",
        "options": {"duration_seconds": 15, "max_budget_usd": 5},
    })
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "completed", data.get("error")
    assert [warning for warning in data["warnings"] if "without a request id: 2" in warning]
    assert all(line["estimated"] for line in data["cost"]["lines"] if line["provider"] == "fal.ai")


async def test_what_a_tool_left_for_the_operator_becomes_a_warning_of_the_job(agent_client, fake_sdk):
    """A voiceover that fell back to a stock voice is a finished, successful call. Only the
    operator needs to know, and the job's warnings are where an operator looks."""
    note = "voiceover_tts used the stock voice Bill: this month's voice creations are used up"
    fake_sdk.notes = {"fal_elevenlabs_tts": note}
    response = await agent_client.post("/v1/videos", json={
        "prompt": "A 15s TikTok ad for example.com with a presenter",
        "options": {"duration_seconds": 15, "max_budget_usd": 5},
    })
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "completed", data.get("error")
    assert data["warnings"].count(note) == 1, data["warnings"]


async def test_the_voices_of_videos_that_are_gone_are_removed_from_elevenlabs(agent_client, monkeypatch):
    """A video's voice is kept on ElevenLabs for as long as the video is kept here, and goes with
    it. Nothing else on the account is touched, and without a key nothing is asked."""
    from tests.tools.eleven_fake import FakeEleven

    manager = agent_client.app.state.manager
    eleven = FakeEleven()
    monkeypatch.setattr("requests.request", eleven.request)
    monkeypatch.setenv("OPENMONTAGE_INSTANCE_ID", "test")
    for name in ("ELEVENLABS_VOICE_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert manager.cleanup_voices() == [] and eleven.calls == []

    monkeypatch.setenv("ELEVENLABS_VOICE_API_KEY", "eleven-test-key")
    job = (await agent_client.post("/v1/videos", json={"prompt": "An ad for example.com", "draft": True})).json()
    kept = eleven.add_voice(f"om-test-{job['id']}", age_seconds=90_000)
    gone = eleven.add_voice("om-test-job_0123456789abcdef", age_seconds=300_000)
    owner = eleven.add_voice("The owner's own narrator", age_seconds=9_000_000, category="professional")
    assert manager.cleanup_voices() == ["job_0123456789abcdef"]
    assert set(eleven.voices) == {kept, owner} and gone not in eleven.voices

    # The video is removed here: its voice goes on the next round.
    manager.store.delete(job["id"])
    assert manager.cleanup_voices() == [job["id"]] and set(eleven.voices) == {owner}

    # ElevenLabs being down is no reason for the cleanup round to fail.
    def down(*_args, **_kwargs):
        raise ConnectionError("down")

    monkeypatch.setattr("requests.request", down)
    assert manager.cleanup_voices() == []


def test_stage_tracker_follows_the_tools_that_ran_and_never_regresses():
    from engine.agent.runner import StageTracker

    tracker = StageTracker()
    assert tracker.update("image_gen") is None, "images are part of the agent's preparation"
    assert StageTracker().update("voiceover_tts") == ("voiceover", 20), "the one-call voiceover is the voice stage"
    assert tracker.update("fal_elevenlabs_tts") == ("voiceover", 20)
    assert tracker.update("fal_avatar") == ("talking shots", 40)
    # Revisiting an earlier stage's tool does not move the label backwards.
    assert tracker.update("tts_selector") is None
    assert tracker.update("transcriber") == ("captions", 55)
    # Buying the music is not mixing it: only the mixer marks the audio mix.
    assert tracker.update("fal_lyria_music") is None
    assert tracker.update("fal_elevenlabs_music") is None
    assert tracker.update("audio_mixer") == ("audio mix", 65)
    assert tracker.update("video_compose") == ("render", 80)


def test_a_batch_that_buys_everything_at_once_does_not_skip_the_bar_ahead():
    """Video 713 (job_ca3eb840021d935a): the paid batch started the voice lines, the stills, the
    clips and the music in the same second, and the bar went from 10% to "audio mix, 65%" before
    a single take existed. This is that job's ledger, in order."""
    from engine.agent.runner import StageTracker

    started = ["openai_image", "fal_elevenlabs_tts", "fal_elevenlabs_tts", "h3_max_video", "h3_max_video",
               "fal_elevenlabs_tts", "fal_elevenlabs_tts", "music_gen", "transcriber", "transcriber",
               "fal_avatar", "fal_avatar", "fal_elevenlabs_tts", "fal_elevenlabs_music", "transcriber",
               "audio_mixer"]
    tracker = StageTracker()
    seen = [stage for stage in (tracker.update(tool) for tool in started) if stage]
    assert seen == [("voiceover", 20), ("talking shots", 40), ("captions", 55), ("audio mix", 65)]


def test_only_a_real_remotion_run_counts_as_a_direct_render():
    from engine.agent.runner import _DIRECT_RENDER

    # The shape a real job ran (job_cfcebe356b5cb69e).
    assert _DIRECT_RENDER.search("cd /app/remotion-composer && P=/data/p && timeout 900 npx remotion render src/index.tsx TalkingHead out.mp4")
    assert _DIRECT_RENDER.search("npx remotion render src/index.tsx")
    # Reading about it is not rendering (job_ed7f82b42067d137 reached "render" at 66 s this way).
    assert not _DIRECT_RENDER.search('grep -n "npx remotion render" tools/video/video_compose.py')
    assert not _DIRECT_RENDER.search("sed -n 1,200p remotion-composer/SCENE_TYPES.md; ls remotion-composer/src")


def test_the_kit_render_counts_as_the_render():
    from engine.agent.runner import _DIRECT_RENDER

    assert _DIRECT_RENDER.search("cd /data/projects/job_x && kit render --name promo")
    assert _DIRECT_RENDER.search("kit render")
    assert _DIRECT_RENDER.search("cd /data/p && env PYTHONPATH=/app python -m lib.videokit render --name x")
    # Sheets, checks and reading about the command are not the render.
    assert not _DIRECT_RENDER.search("kit sheet --draft")
    assert not _DIRECT_RENDER.search("kit check && kit sheet")
    assert not _DIRECT_RENDER.search('grep -n "kit render" AGENT_GUIDE.md')


def test_word_timing_before_the_takes_is_not_the_captions_stage():
    from engine.agent.runner import StageTracker

    tracker = StageTracker()
    assert tracker.update("fal_elevenlabs_tts") == ("voiceover", 20)
    # `kit voice` times the words right after the lines are spoken; the takes are still to come,
    # and a client must not be shown them as done.
    assert tracker.update("transcriber") is None
    assert tracker.update("fal_avatar") == ("talking shots", 40)
    assert tracker.update("audio_mixer") == ("audio mix", 65)
    assert tracker.update("video_compose") == ("render", 80)


async def test_reading_about_a_tool_does_not_move_the_stage(settings, monkeypatch):
    import dataclasses

    import httpx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    read, ran = asyncio.Event(), asyncio.Event()
    proceed = asyncio.Event()

    async def fake_query(*, prompt, options=None, **_):
        yield SystemMessage(subtype="init", data={"session_id": "s", "apiKeySource": "ANTHROPIC_API_KEY",
                                                  "model": options.model, "tools": ["Bash"]})
        yield AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={
            "command": "sed -n 1,50p tools/avatar/fal_avatar.py; cat schemas/remotion.json; grep video_compose x"})],
            model=options.model)
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="source code", is_error=False)])
        read.set()
        await proceed.wait()
        record_tool_call(options, "fal_avatar", 0.88, output="/p/take1.mp4")
        yield UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="True None 0.88", is_error=False)])
        ran.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(agent_session, "sdk_query", fake_query)
    app = create_app(settings=dataclasses.replace(settings, agent_wall_time_minutes=0.2))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Authorization": "Bearer test-token"}) as http:
            job_id = (await http.post("/v1/videos", json={"prompt": "reads a lot first"})).json()["id"]
            await asyncio.wait_for(read.wait(), 5)
            data = (await http.get(f"/v1/jobs/{job_id}?transcript_limit=0")).json()
            assert data["stage"] == "agent" and data["progress"] == 10

            proceed.set()
            await asyncio.wait_for(ran.wait(), 5)
            data = (await http.get(f"/v1/jobs/{job_id}?transcript_limit=0")).json()
            assert data["stage"] == "talking shots" and data["progress"] == 40
            await http.delete(f"/v1/jobs/{job_id}")


async def test_references_reach_the_brief(agent_client, fake_sdk):
    upload = await agent_client.post("/v1/assets", files={"file": ("mia.jpg", b"jpeg-bytes", "image/jpeg")})
    assert upload.status_code in (200, 201), upload.text
    asset_id = upload.json()["id"]

    response = await agent_client.post("/v1/videos", json={
        "prompt": "video with a presenter reference",
        "references": [{"asset_id": asset_id, "role": "presenter", "label": "Mia the influencer"}],
    })
    assert response.status_code == 202, response.text
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "completed", data.get("error")

    brief = fake_sdk.calls[-1]["prompt"]
    match = re.search(r"- (\S+mia\.jpg) - role: presenter - Mia the influencer", brief)
    assert match, brief
    copied = agent_session.Path(match.group(1))
    assert copied.is_file() and copied.read_bytes() == b"jpeg-bytes"


async def test_stage_progression_reaches_talking_shots(agent_client, fake_sdk):
    fake_sdk.delay = 0.3
    response = await agent_client.post("/v1/videos", json={"prompt": "stage probe video"})
    job_id = response.json()["id"]
    seen = set()
    for _ in range(100):
        data = (await agent_client.get(f"/v1/jobs/{job_id}?transcript_limit=0")).json()
        seen.add(data["stage"])
        if data["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.02)
    assert data["status"] == "completed"
    assert "talking shots" in seen  # the tool-call heuristic mapped fal_avatar


async def test_agent_failure_fails_the_job(agent_client, fake_sdk):
    fake_sdk.subtype = "error_max_budget"
    response = await agent_client.post("/v1/videos", json={"prompt": "doomed video"})
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "failed"
    assert data["error"]["code"] == "agent_failed"
    assert "budget exceeded" in data["error"]["message"]
    # The claude spend is still accounted even when the run fails.
    assert any(line["item"] == "claude agent" for line in data["cost"]["lines"])


async def test_a_job_that_cannot_get_a_box_fails_instead_of_running_unboxed(settings, fake_sdk, monkeypatch):
    import dataclasses

    import httpx

    from engine.sandbox import box as sandbox_box

    monkeypatch.setattr(sandbox_box, "sandbox_problem", lambda: "bubblewrap is not installed")
    app = create_app(settings=dataclasses.replace(settings, agent_sandbox="required", agent_wall_time_minutes=0.05))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers={"Authorization": "Bearer test-token"}) as client:
            response = await client.post("/v1/videos", json={"prompt": "a video that needs a box"})
            data = await _wait_terminal(client, response.json()["id"])
    assert data["status"] == "failed"
    assert data["error"]["code"] == "agent_unavailable"
    assert "bubblewrap is not installed" in data["error"]["message"]
    assert not any(line["item"] == "claude agent" for line in data["cost"]["lines"])  # no session ran


async def test_auth_mismatch_fails_closed(agent_client, fake_sdk):
    fake_sdk.api_key_source = "none"
    response = await agent_client.post("/v1/videos", json={"prompt": "auth probe video"})
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "failed"
    assert data["error"]["code"] == "agent_auth"


async def test_wall_time_kill(agent_client, fake_sdk):
    fake_sdk.hang = True  # agent_wall_time_minutes=0.05 -> 3 s; asyncio.timeout(30) covers it
    response = await agent_client.post("/v1/videos", json={"prompt": "hanging video"})
    data = await _wait_terminal(agent_client, response.json()["id"], timeout=30)
    assert data["status"] == "failed"
    assert data["error"]["code"] == "agent_timeout"


async def test_revise_resumes_the_parent_session(agent_client, fake_sdk):
    created = await agent_client.post("/v1/videos", json={"prompt": "original video"})
    parent = await _wait_terminal(agent_client, created.json()["id"])
    assert parent["status"] == "completed"

    revised = await agent_client.post(f"/v1/jobs/{parent['id']}/revise",
                                      json={"feedback": "Make the hook text bigger"})
    assert revised.status_code == 202, revised.text
    data = await _wait_terminal(agent_client, revised.json()["id"])
    assert data["status"] == "completed", data.get("error")

    assert len(fake_sdk.calls) == 2
    assert fake_sdk.calls[1]["options"].resume == "sess-1"
    assert fake_sdk.calls[1]["options"].fork_session is True, "the parent's session stays as it finished"
    assert fake_sdk.calls[0]["options"].fork_session is False
    assert fake_sdk.calls[1]["options"].env["CLAUDE_CONFIG_DIR"] == fake_sdk.calls[0]["options"].env["CLAUDE_CONFIG_DIR"], \
        "the revision finds the parent's session"
    assert "Make the hook text bigger" in fake_sdk.calls[1]["prompt"]
    assert data["result"]["video"]["name"] == "video_rev2.mp4"  # the NEW export wins


async def _stop_after_the_paid_calls(client, fake_sdk, prompt="a video to stop"):
    fake_sdk.delay = 30  # the session waits after its two paid calls, before exporting
    created = await client.post("/v1/videos", json={"prompt": prompt})
    job_id = created.json()["id"]
    async with asyncio.timeout(10):
        while (await client.get(f"/v1/jobs/{job_id}")).json()["cost"]["spent_usd"] < 0.9:
            await asyncio.sleep(0.02)
    stopping = await client.post(f"/v1/jobs/{job_id}/stop")
    assert stopping.status_code == 200, stopping.text
    fake_sdk.delay = 0
    return await _wait_terminal(client, job_id)


async def test_a_stopped_job_keeps_its_work_and_its_cost(agent_client, fake_sdk):
    data = await _stop_after_the_paid_calls(agent_client, fake_sdk)
    assert data["status"] == "cancelled" and data["stopped"] is True
    assert data["error"]["code"] == "stopped"
    assert data["cost"]["spent_usd"] >= 0.9, "the work done before the stop stays on the bill"
    project = agent_session.Path(_project_dir(fake_sdk.calls[0]["prompt"]))
    assert project.is_dir()


async def test_resuming_continues_the_stopped_session_in_its_project(agent_client, fake_sdk):
    stopped = await _stop_after_the_paid_calls(agent_client, fake_sdk)
    resumed = await agent_client.post(f"/v1/jobs/{stopped['id']}/resume", json={"options": {"max_budget_usd": 2}})
    assert resumed.status_code == 202, resumed.text
    data = await _wait_terminal(agent_client, resumed.json()["id"])
    assert data["status"] == "completed", data.get("error")
    assert data["kind"] == "resume" and data["parent_job_id"] == stopped["id"]
    options = fake_sdk.calls[-1]["options"]
    assert options.resume == "sess-1" and options.fork_session is True
    assert "stopped this run" in fake_sdk.calls[-1]["prompt"]
    assert data["result"]["video"]["name"] == "video_rev2.mp4"
    # The resume bills its own run only: the stopped job's paid calls are not counted again.
    assert all(line["item"] != "fal_elevenlabs_tts eleven-v3 (vo.mp3)" or line["usd"] == 0.02
               for line in data["cost"]["lines"])
    again = await agent_client.post(f"/v1/jobs/{data['id']}/resume", json={})
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_resumable"


async def test_only_a_stopped_job_can_be_resumed(agent_client, fake_sdk):
    created = await agent_client.post("/v1/videos", json={"prompt": "a finished video"})
    finished = await _wait_terminal(agent_client, created.json()["id"])
    refused = await agent_client.post(f"/v1/jobs/{finished['id']}/resume", json={})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "not_resumable"


async def test_a_stopped_job_is_kept_until_it_is_resumed_or_purged(agent_client, fake_sdk):
    from datetime import timedelta

    from engine.jobs.models import utcnow

    stopped = await _stop_after_the_paid_calls(agent_client, fake_sdk)
    created = await agent_client.post("/v1/videos", json={"prompt": "an old finished video"})
    old = await _wait_terminal(agent_client, created.json()["id"])
    app = agent_client.app
    for job_id in (stopped["id"], old["id"]):
        job = app.state.store.get(job_id)
        job.finished_at = utcnow() - timedelta(days=30)
        app.state.store.save(job, force=True)
    app.state.manager.cleanup_expired()
    assert app.state.store.get(stopped["id"]) is not None, "a stopped job waits for its resume"
    assert app.state.store.get(old["id"]) is None, "a finished job still expires"

    purged = await agent_client.delete(f"/v1/jobs/{stopped['id']}?purge=true")
    assert purged.status_code == 200 and purged.json() == {"id": stopped["id"], "deleted": True}
    assert (await agent_client.get(f"/v1/jobs/{stopped['id']}")).status_code == 404
    assert not agent_session.Path(_project_dir(fake_sdk.calls[0]["prompt"])).exists()


async def test_revise_ledger_counts_only_the_revisions_own_spend(settings, monkeypatch):
    """A revision works in the first job's project and keeps logging to the same cost log. If the
    tool ledger is ever empty, that log is the fallback, and it must not re-count the parent's calls."""
    import dataclasses

    import httpx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("FAL_KEY", "test-fal-key")
    prompts: list[str] = []

    async def fake_query(*, prompt, options=None, **_):
        prompts.append(prompt)
        session_id = f"sess-{len(prompts)}"
        yield SystemMessage(subtype="init", data={
            "session_id": session_id, "apiKeySource": "ANTHROPIC_API_KEY",
            "model": options.model, "tools": ["Bash", "Read"]})
        project = agent_session.Path(_project_dir(prompts[0]))
        log = project / "artifacts" / "cost_log.json"
        log.parent.mkdir(parents=True, exist_ok=True)
        entries = json.loads(log.read_text(encoding="utf-8")) if log.is_file() else []
        if options.resume:  # the revise prompt: "keep logging costs to .../cost_log.json"
            entries.append({"item": "fal_elevenlabs_tts new hook line", "usd": 0.01})
            name = "video_rev2.mp4"
        else:
            entries += [{"item": "fal_avatar take1", "usd": 0.88},
                        {"item": "fal_elevenlabs_tts voiceover", "usd": 0.02}]
            name = "video.mp4"
        log.write_text(json.dumps(entries), encoding="utf-8")
        exports = project / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        (exports / name).write_bytes(b"fake-mp4-bytes")
        (project / "RUN_REPORT.md").write_text("# report", encoding="utf-8")
        write_posting(project)
        yield AssistantMessage(content=[TextBlock(text="Exported the final video.")], model=options.model)
        yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=10, is_error=False,
                            num_turns=2, session_id=session_id, total_cost_usd=0.05 + 0.03 * (len(prompts) - 1),
                            result="done")

    monkeypatch.setattr(agent_session, "sdk_query", fake_query)
    app = create_app(settings=dataclasses.replace(settings, agent_wall_time_minutes=0.05))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Authorization": "Bearer test-token"}) as http:
            created = await http.post("/v1/videos", json={"prompt": "original video"})
            parent = await _wait_terminal(http, created.json()["id"])
            assert parent["status"] == "completed", parent.get("error")
            paid = sorted((line["item"], line["usd"]) for line in parent["cost"]["lines"] if line["provider"] != "anthropic")
            assert paid == [("fal_avatar take1", 0.88), ("fal_elevenlabs_tts voiceover", 0.02)]
            assert any("tool ledger recorded nothing" in warning for warning in parent["warnings"])

            revised = await http.post(f"/v1/jobs/{parent['id']}/revise", json={"feedback": "A new hook line"})
            child = await _wait_terminal(http, revised.json()["id"])
            assert child["status"] == "completed", child.get("error")
            paid = [(line["item"], line["usd"]) for line in child["cost"]["lines"] if line["provider"] != "anthropic"]
            assert paid == [("fal_elevenlabs_tts new hook line", 0.01)], "the parent's calls are not billed again"
            assert child["cost"]["spent_usd"] == round(0.01 + 0.03, 4)


class RevisionSDK:
    """Parent and revisions in one project. Sessions behave as the real SDK's do (measured): cost is
    cumulative across resumes, and a fork gets a new id and includes the session it forked but not
    other forks of it. Each run's tools record into that job's own ledger."""

    def __init__(self):
        self.prompts: list[str] = []
        self.export = True
        self.session_cost: dict[str, float] = {}

    def install(self, monkeypatch):
        fake = self

        async def fake_query(*, prompt, options=None, **_):
            fake.prompts.append(prompt)
            run = len(fake.prompts)
            session_id = options.resume if options.resume and not options.fork_session else f"sess-{run}"
            yield SystemMessage(subtype="init", data={
                "session_id": session_id, "apiKeySource": "ANTHROPIC_API_KEY",
                "model": options.model, "tools": ["Bash", "Read"]})
            project = agent_session.Path(_project_dir(fake.prompts[0]))
            exports = project / "exports"
            exports.mkdir(parents=True, exist_ok=True)
            if options.resume:
                record_tool_call(options, "fal_elevenlabs_tts", 0.01, model="eleven-v3", output=f"/p/hook{run}.mp3")
                if fake.export:
                    (exports / f"video_rev{run}.mp4").write_bytes(b"fake-mp4-bytes")
                    write_posting(project)
            else:
                record_tool_call(options, "fal_avatar", 0.88, model="h3max-acting", output="/p/take1.mp4")
                (exports / "video.mp4").write_bytes(b"fake-mp4-bytes")
                write_posting(project)
            yield AssistantMessage(content=[TextBlock(text="Done with this run.")], model=options.model)
            total = round(fake.session_cost.get(options.resume, 0.0) + (0.03 if options.resume else 0.20), 4)
            fake.session_cost[session_id] = total
            yield ResultMessage(subtype="success", duration_ms=10, duration_api_ms=10, is_error=False, num_turns=2,
                                session_id=session_id, total_cost_usd=total, result="done")

        monkeypatch.setattr(agent_session, "sdk_query", fake_query)
        return fake


async def test_revisions_pay_only_their_own_claude_and_tool_spend(settings, monkeypatch):
    import dataclasses

    import httpx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    RevisionSDK().install(monkeypatch)
    app = create_app(settings=dataclasses.replace(settings, agent_wall_time_minutes=0.05))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Authorization": "Bearer test-token"}) as http:
            created = await http.post("/v1/videos", json={"prompt": "original video"})
            parent = await _wait_terminal(http, created.json()["id"])
            assert parent["status"] == "completed", parent.get("error")
            assert parent["cost"]["spent_usd"] == round(0.20 + 0.88, 4)

            revised = await http.post(f"/v1/jobs/{parent['id']}/revise", json={"feedback": "A new hook"})
            first = await _wait_terminal(http, revised.json()["id"])
            assert first["status"] == "completed", first.get("error")
            items = {line["item"]: line["usd"] for line in first["cost"]["lines"]}
            assert items == {"claude agent": 0.03, "fal_elevenlabs_tts eleven-v3 (hook2.mp3)": 0.01}
            assert first["result"]["agent"]["cost_usd"] == 0.23, "the session total stays cumulative for the next link"

            # A revision of the revision subtracts everything the chain already paid for.
            again = await http.post(f"/v1/jobs/{first['id']}/revise", json={"feedback": "Shorter"})
            second = await _wait_terminal(http, again.json()["id"])
            assert second["status"] == "completed", second.get("error")
            items = {line["item"]: line["usd"] for line in second["cost"]["lines"]}
            assert items == {"claude agent": 0.03, "fal_elevenlabs_tts eleven-v3 (hook3.mp3)": 0.01}
            assert second["result"]["video"]["name"] == "video_rev3.mp4"


async def test_a_revision_with_no_new_export_fails_instead_of_returning_the_parents_video(settings, monkeypatch):
    import dataclasses

    import httpx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    sdk = RevisionSDK().install(monkeypatch)
    app = create_app(settings=dataclasses.replace(settings, agent_wall_time_minutes=0.05))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Authorization": "Bearer test-token"}) as http:
            created = await http.post("/v1/videos", json={"prompt": "original video"})
            parent = await _wait_terminal(http, created.json()["id"])
            assert parent["status"] == "completed", parent.get("error")

            sdk.export = False
            revised = await http.post(f"/v1/jobs/{parent['id']}/revise", json={"feedback": "A new hook"})
            child = await _wait_terminal(http, revised.json()["id"])
            assert child["status"] == "failed" and child["error"]["code"] == "no_video"
            assert not child.get("result")
            assert len(sdk.prompts) == 3, "the parent, the revision and its one resume"

            # The failed attempt spent Claude money, but the member's next revision doesn't pay for it.
            sdk.export = True
            again = await http.post(f"/v1/jobs/{parent['id']}/revise", json={"feedback": "A new hook"})
            retried = await _wait_terminal(http, again.json()["id"])
            assert retried["status"] == "completed", retried.get("error")
            items = {line["item"]: line["usd"] for line in retried["cost"]["lines"]}
            assert items["claude agent"] == 0.03


async def test_a_session_that_stops_without_a_video_is_resumed_once(agent_client, fake_sdk):
    fake_sdk.export_from_call = 2
    response = await agent_client.post("/v1/videos", json={"prompt": "stops early, then finishes"})
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "completed", data.get("error")

    assert len(fake_sdk.calls) == 2
    resumed = fake_sdk.calls[1]
    assert resumed["options"].resume == "sess-1", "the same session, with everything it already bought"
    assert resumed["options"].fork_session is False
    assert "no person is present" in resumed["prompt"] and "without paying for it again" in resumed["prompt"]
    assert any("resumed once" in warning for warning in data["warnings"])
    assert data["result"]["video"]["name"] == "video_rev2.mp4"


async def test_a_session_that_never_exports_fails_after_the_one_resume(agent_client, fake_sdk):
    fake_sdk.export_from_call = 99
    response = await agent_client.post("/v1/videos", json={"prompt": "never exports"})
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "failed" and data["error"]["code"] == "no_video"
    assert data["error"]["retryable"] is True
    assert len(fake_sdk.calls) == 2, "one resume, never a loop"
    assert data["stage"] != "collecting", "nothing was there to collect"


async def test_a_failed_session_is_not_resumed(agent_client, fake_sdk):
    fake_sdk.subtype = "error_max_turns"
    response = await agent_client.post("/v1/videos", json={"prompt": "ran out of turns"})
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["error"]["code"] == "agent_failed" and len(fake_sdk.calls) == 1


async def test_the_resume_shares_the_jobs_deadline(agent_client, fake_sdk):
    fake_sdk.export_from_call = 2
    fake_sdk.hang_from_call = 2     # agent_wall_time_minutes=0.05 -> 3 s for the whole job
    response = await agent_client.post("/v1/videos", json={"prompt": "the resume hangs"})
    data = await _wait_terminal(agent_client, response.json()["id"], timeout=30)
    assert data["status"] == "failed" and data["error"]["code"] == "agent_timeout"
    assert len(fake_sdk.calls) == 2


async def test_ten_parallel_video_jobs(agent_client, fake_sdk):
    fake_sdk.delay = 0.2
    responses = await asyncio.gather(*[
        agent_client.post("/v1/videos", json={"prompt": f"parallel probe video {i}"})
        for i in range(10)])
    ids = [r.json()["id"] for r in responses]
    assert all(r.status_code in (200, 202) for r in responses)

    results = await asyncio.gather(*[_wait_terminal(agent_client, jid, timeout=30) for jid in ids])
    assert [r["status"] for r in results] == ["completed"] * 10
    assert len(fake_sdk.calls) == 10
    # Every job got its own project dir, session and video file.
    assert len({r["result"]["session_id"] for r in results}) == 10
    for jid in ids:
        files = (await agent_client.get(f"/v1/jobs/{jid}/files")).json()["files"]
        assert any(f["name"] == "video.mp4" for f in files)


# -- what a video is posted with: its cover picture and its post text ------------------------------
async def test_a_finished_video_comes_with_its_cover_and_its_post_text(agent_client, fake_sdk):
    response = await agent_client.post("/v1/videos", json={"prompt": "A cold brew ad"})
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "completed", data.get("error")
    assert data["result"]["post"] == POST
    assert data["result"]["cover"]["name"] == "cover.jpg" and data["result"]["cover"]["content_type"] == "image/jpeg"
    # The second of the video the frame is from, as `kit cover` recorded it; the file list stays plain.
    assert data["result"]["cover"]["at_seconds"] == 1.6
    listed = {key: value for key, value in data["result"]["files"]["cover"].items() if key != "url"}
    assert listed == {key: value for key, value in data["result"]["cover"].items() if key != "at_seconds"}
    assert set(listed) == {"name", "size_bytes", "content_type"}
    cover = await agent_client.get(f"/v1/jobs/{data['id']}/files/cover.jpg")
    assert cover.status_code == 200 and cover.content.startswith(b"\xff\xd8\xff")
    assert len(fake_sdk.calls) == 1 and data["warnings"] == []
    brief = fake_sdk.calls[0]["prompt"].replace("\\", "/")
    assert "exports/cover.jpg" in brief and "artifacts/post.json" in brief and "`kit cover --at <second>`" in brief


async def test_a_video_without_post_text_gets_one_follow_up_in_its_session(agent_client, fake_sdk):
    """A finished video is never failed or made again for its post text: the same session is asked
    once, and what that cost is on the job."""
    fake_sdk.write_post = False
    response = await agent_client.post("/v1/videos", json={"prompt": "A cold brew ad", "options": {"language": "es"}})
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "completed", data.get("error")
    assert len(fake_sdk.calls) == 2
    follow = fake_sdk.calls[1]
    assert follow["options"].resume == "sess-1" and follow["options"].fork_session is False
    assert "post.json" in follow["prompt"] and "language (es)" in follow["prompt"]
    assert "change nothing else in the project" in follow["prompt"]
    assert data["result"]["post"] == POST and data["result"]["video"]["name"] == "video.mp4"
    assert not any("post text" in warning for warning in data["warnings"])
    # A resumed session reports what it cost since it began: the follow-up's figure is the job's.
    items = {line["item"]: line["usd"] for line in data["cost"]["lines"]}
    assert items["claude agent"] == 0.24 and data["result"]["agent"]["cost_usd"] == 0.24


async def test_a_follow_up_that_writes_nothing_leaves_the_video_and_says_so(agent_client, fake_sdk):
    fake_sdk.write_post = False
    fake_sdk.answer_post = False
    response = await agent_client.post("/v1/videos", json={"prompt": "A cold brew ad"})
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "completed", data.get("error")
    assert len(fake_sdk.calls) == 2, "one follow-up, never a loop"
    assert data["result"]["post"] is None and data["result"]["video"]["name"] == "video.mp4"
    assert any("has no post text" in warning for warning in data["warnings"])


async def test_the_covers_second_is_named_only_when_it_is_this_videos(agent_client, fake_sdk):
    """A record of another export, or none at all (a cover made by other means): the picture is still
    returned, without a second. A revision's cover carries the second recorded for the revised export."""
    fake_sdk.cover_of = "an_earlier_cut.mp4"
    response = await agent_client.post("/v1/videos", json={"prompt": "A cold brew ad"})
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "completed", data.get("error")
    assert data["result"]["cover"]["name"] == "cover.jpg" and data["result"]["cover"]["at_seconds"] is None

    fake_sdk.cover_of, fake_sdk.cover_at = None, 2.25
    revised = await agent_client.post(f"/v1/jobs/{data['id']}/revise", json={"feedback": "A bigger hook"})
    again = await _wait_terminal(agent_client, revised.json()["id"])
    assert again["status"] == "completed", again.get("error")
    assert again["result"]["video"]["name"] == "video_rev2.mp4" and again["result"]["cover"]["at_seconds"] == 2.25

    fake_sdk.cover_at = None
    response = await agent_client.post("/v1/videos", json={"prompt": "Another cold brew ad"})
    plain = await _wait_terminal(agent_client, response.json()["id"])
    assert plain["result"]["cover"]["name"] == "cover.jpg" and plain["result"]["cover"]["at_seconds"] is None


async def test_a_video_whose_cover_cannot_be_made_still_completes(agent_client, fake_sdk):
    """The director made no cover, and the fake export is no video a frame could be taken from."""
    fake_sdk.write_cover = False
    response = await agent_client.post("/v1/videos", json={"prompt": "A cold brew ad"})
    data = await _wait_terminal(agent_client, response.json()["id"])
    assert data["status"] == "completed", data.get("error")
    assert data["result"]["cover"] is None and "cover" not in data["result"]["files"]
    assert any(warning.startswith("no cover picture") for warning in data["warnings"])


# -- the style of a video ---------------------------------------------------------------------------
async def test_the_mention_style_reaches_the_brief_and_a_revision_keeps_it(agent_client, fake_sdk):
    created = await agent_client.post("/v1/videos", json={"prompt": "A cold brew ad", "options": {"style": "mention"}})
    assert created.status_code == 202, created.text
    parent = await _wait_terminal(agent_client, created.json()["id"])
    assert parent["status"] == "completed", parent.get("error")
    brief = fake_sdk.calls[0]["prompt"]
    assert "## The style of this video: a mention (no call to action, no offer)" in brief
    assert "like the video, it carries no call to action and no offer wording" in brief

    revised = await agent_client.post(f"/v1/jobs/{parent['id']}/revise", json={"feedback": "A bigger hook"})
    child = await _wait_terminal(agent_client, revised.json()["id"])
    assert child["status"] == "completed", child.get("error")
    assert "This video is in the mention style" in fake_sdk.calls[1]["prompt"]


async def test_a_video_is_a_direct_ad_unless_asked_otherwise(agent_client, fake_sdk):
    created = await agent_client.post("/v1/videos", json={"prompt": "A cold brew ad"})
    parent = await _wait_terminal(agent_client, created.json()["id"])
    brief = fake_sdk.calls[0]["prompt"]
    assert "The style of this video" not in brief and "it may end on the video's call to action" in brief
    revised = await agent_client.post(f"/v1/jobs/{parent['id']}/revise", json={"feedback": "A bigger hook"})
    await _wait_terminal(agent_client, revised.json()["id"])
    assert "mention style" not in fake_sdk.calls[1]["prompt"]
    refused = await agent_client.post("/v1/videos", json={"prompt": "x y z", "options": {"style": "testimonial"}})
    assert refused.status_code == 422


# -- the live view: every step in plain words -------------------------------------------------------
async def test_the_chat_stream_carries_every_step_in_plain_words(agent_client, fake_sdk):
    response = await agent_client.post("/v1/videos", json={"prompt": "A cold brew ad"})
    data = await _wait_terminal(agent_client, response.json()["id"])
    steps: dict[str, list[dict]] = {}
    for event in data["transcript"]["events"]:
        if event["kind"] == "activity":
            steps.setdefault(event["id"], []).append(event)
    assert steps, "the stream has activity entries"
    labels = {events[-1]["label"] for events in steps.values()}
    # The two generation calls come from the tool desk's ledger, with their own start and end.
    assert {"Recording the voice", "Filming take 1"} <= labels
    for events in steps.values():
        assert events[0]["state"] == "started" and events[-1]["state"] in ("done", "failed", "gone"), events
        assert "seconds" in events[-1] or events[-1]["state"] == "gone"
        for event in events:
            assert set(event) <= {"at", "kind", "id", "state", "code", "label", "seconds", "age"}
            # Never a tool, a model, a provider, a file, a command or money.
            assert not re.search(r"fal|eleven|h3max|python|\.mp[34]|\.py|/|\$|_", event["label"]), event
    # The raw calls are still there for the operator, and the client's two kinds beside them.
    kinds = {event["kind"] for event in data["transcript"]["events"]}
    assert {"assistant", "tool", "tool_result", "activity"} <= kinds
