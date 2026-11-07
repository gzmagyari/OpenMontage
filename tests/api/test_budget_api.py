"""POST /v1/jobs/{id}/budget, and live cost accounting through a fake SDK stream."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import httpx
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

import engine.agent.runner as agent_runner
import engine.agent.session as agent_session
from api.main import create_app
from lib import job_ledger
from tests.api.helpers import ledger_dir, record_tool_call

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------- the endpoint

async def test_budget_endpoint_on_running_draft_and_terminal(client, fake_runners):
    submitted = (await client.post("/v1/videos", json={
        "prompt": "re-aim me", "options": {"max_budget_usd": 5}})).json()
    job_id = submitted["id"]

    response = await client.post(f"/v1/jobs/{job_id}/budget", json={"max_budget_usd": 1.25})
    assert response.status_code == 200
    assert client.app.state.store.get(job_id).request["options"]["max_budget_usd"] == 1.25

    response = await client.post(f"/v1/jobs/{job_id}/budget", json={"max_budget_usd": 0})
    assert response.status_code == 422

    draft = (await client.post("/v1/videos", json={"draft": True})).json()
    response = await client.post(f"/v1/jobs/{draft['id']}/budget", json={"max_budget_usd": 2})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_adjustable"

    fake_runners.gate(job_id).set()
    for _ in range(200):
        data = (await client.get(f"/v1/jobs/{job_id}")).json()
        if data["status"] in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.02)
    response = await client.post(f"/v1/jobs/{job_id}/budget", json={"max_budget_usd": 2})
    assert response.status_code == 409

    missing = await client.post("/v1/jobs/job_nope/budget", json={"max_budget_usd": 2})
    assert missing.status_code == 404


# ------------------------------------------- live accounting via the real runner

class MeteredSDK:
    """A stream whose paid calls go through the job ledger like real tools (printing what the agent
    really prints), gated midway so the test can observe the LIVE ledger before the run finishes."""

    def __init__(self):
        self.mid_run = asyncio.Event()
        self.resume = asyncio.Event()
        self.stopped = asyncio.Event()
        self.options = None
        self.cost_log_usd = 0.88
        self.at_mid_run = None

    def install(self, monkeypatch):
        fake = self

        async def fake_query(*, prompt, options=None, **_):
            fake.options = options
            yield SystemMessage(subtype="init", data={
                "session_id": "sess-live", "apiKeySource": "ANTHROPIC_API_KEY",
                "model": options.model, "tools": ["Bash"]})
            yield AssistantMessage(
                content=[TextBlock(text="Buying the voiceover."),
                         ToolUseBlock(id="t1", name="Bash",
                                      input={"command": "python -c \"t=registry._tools['fal_elevenlabs_tts']\""})],
                model=options.model, usage={"input_tokens": 200_000, "output_tokens": 10_000})
            record_tool_call(options, "fal_elevenlabs_tts", 0.02, model="eleven-v3", output="/p/vo.mp3")
            yield UserMessage(content=[ToolResultBlock(tool_use_id="t1", is_error=False, content="True None 0.02")])
            if fake.at_mid_run:
                fake.at_mid_run(options)
            fake.mid_run.set()
            try:
                await fake.resume.wait()
            except asyncio.CancelledError:
                fake.stopped.set()
                raise
            yield AssistantMessage(
                content=[ToolUseBlock(id="t2", name="Bash",
                                      input={"command": "python -c \"t=registry._tools['fal_avatar']\""})],
                model=options.model)
            call = record_tool_call(options, "fal_avatar", 0.88, model="h3max-acting", output="/p/take1.mp4")
            yield UserMessage(content=[ToolResultBlock(
                tool_use_id="t2", is_error=False, content=call.denied or "True None 0.88")])
            import re as _re
            project = _re.search(r"All project work goes in: (.+)", prompt).group(1).strip()
            exports = agent_session.Path(project) / "exports"
            exports.mkdir(parents=True, exist_ok=True)
            (exports / "video.mp4").write_bytes(b"fake-mp4-bytes")
            artifacts = agent_session.Path(project) / "artifacts"
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / "cost_log.json").write_text(json.dumps([
                {"item": "fal_elevenlabs_tts voiceover", "usd": 0.02},
                {"item": "fal_avatar h3max-acting take1", "usd": fake.cost_log_usd},
            ]), encoding="utf-8")
            yield AssistantMessage(content=[TextBlock(text="Exported.")], model=options.model)
            yield ResultMessage(subtype="success", duration_ms=900, duration_api_ms=800,
                                is_error=False, num_turns=4, session_id="sess-live",
                                total_cost_usd=0.21, result="done")

        monkeypatch.setattr(agent_session, "sdk_query", fake_query)
        return fake


@pytest_asyncio.fixture()
async def metered(settings, monkeypatch):
    """(http client, fake SDK) on the real agent runners, the live refresh sped up for the test."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("FAL_KEY", "test-fal-key")
    monkeypatch.setattr(agent_runner, "_WATCH_SECONDS", 0.02)
    sdk = MeteredSDK().install(monkeypatch)
    app = create_app(settings=dataclasses.replace(settings, agent_wall_time_minutes=0.2))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Authorization": "Bearer test-token"}) as http:
            yield http, sdk


async def _poll(http, job_id):
    return (await http.get(f"/v1/jobs/{job_id}", params={"transcript_limit": 0})).json()


async def _wait_for(check, what, http, job_id):
    for _ in range(300):
        data = await _poll(http, job_id)
        if check(data):
            return data
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}: {data}")


def _terminal(data):
    return data["status"] in ("completed", "failed", "cancelled")


async def test_live_ledger_is_the_tools_recorded_spend(metered):
    http, sdk = metered
    job_id = (await http.post("/v1/videos", json={
        "prompt": "a metered run", "options": {"duration_seconds": 15, "max_budget_usd": 5}})).json()["id"]

    await asyncio.wait_for(sdk.mid_run.wait(), 5)
    live = await _poll(http, job_id)
    lines = {line["item"]: line for line in live["cost"]["lines"]}
    assert lines["fal_elevenlabs_tts eleven-v3 (vo.mp3)"]["usd"] == 0.02, "tool spend shows while running"
    claude = lines["claude agent"]
    assert claude["estimated"] is True and claude["usd"] == 0.075  # 0.2M in + 10k out at the live rates
    assert live["cost"]["spent_usd"] == 0.095

    # The session runs with the ledger folder set and no SDK hook, so stdin is never needed again.
    assert job_ledger.read_budget(ledger_dir(sdk.options)) == 5
    assert sdk.options.hooks is None

    sdk.resume.set()
    done = await _wait_for(_terminal, "the end of the job", http, job_id)
    assert done["status"] == "completed"
    items = {line["item"]: line["usd"] for line in done["cost"]["lines"]}
    assert items == {"claude agent": 0.21, "fal_elevenlabs_tts eleven-v3 (vo.mp3)": 0.02,
                     "fal_avatar h3max-acting (take1.mp4)": 0.88}
    assert next(line for line in done["cost"]["lines"] if line["item"] == "claude agent")["estimated"] is False
    assert done["cost"]["spent_usd"] == round(0.02 + 0.88 + 0.21, 4)
    assert done["warnings"] == [], "the agent's cost log agrees with the recorded spend"


async def test_the_recorded_spend_is_billed_when_the_cost_log_disagrees(metered):
    http, sdk = metered
    sdk.cost_log_usd = 1.50
    sdk.resume.set()
    job_id = (await http.post("/v1/videos", json={"prompt": "a padded cost log"})).json()["id"]
    done = await _wait_for(_terminal, "the end of the job", http, job_id)
    assert done["status"] == "completed"
    assert done["cost"]["spent_usd"] == round(0.02 + 0.88 + 0.21, 4)
    assert any("differs from the tools' recorded spend" in warning for warning in done["warnings"])


async def test_a_long_tool_call_shows_while_it_runs(metered):
    """A slow foreground call produces no messages; the watch loop still shows it."""
    http, sdk = metered
    sdk.at_mid_run = lambda options: job_ledger.begin_call(
        ledger_dir(options), tool="fal_avatar", provider="fal.ai",
        inputs={"output_path": "/p/take2.mp4"}, estimate_usd=0.79)
    job_id = (await http.post("/v1/videos", json={"prompt": "a slow render"})).json()["id"]
    live = await _wait_for(lambda data: data["cost"]["spent_usd"] > 0.8, "the running call in the ledger", http, job_id)
    items = {line["item"]: line["usd"] for line in live["cost"]["lines"]}
    assert items["fal_avatar (take2.mp4) (not finished, estimated)"] == 0.79
    sdk.resume.set()
    await _wait_for(_terminal, "the end of the job", http, job_id)


async def test_lowering_the_budget_reaches_the_tools_and_a_denial_surfaces(metered):
    http, sdk = metered
    job_id = (await http.post("/v1/videos", json={
        "prompt": "clamped midway", "options": {"max_budget_usd": 5}})).json()["id"]
    await asyncio.wait_for(sdk.mid_run.wait(), 5)

    assert (await http.post(f"/v1/jobs/{job_id}/budget", json={"max_budget_usd": 0.02})).status_code == 200
    directory = ledger_dir(sdk.options)
    for _ in range(300):
        if job_ledger.read_budget(directory) == 0.02:
            break
        await asyncio.sleep(0.02)
    assert job_ledger.read_budget(directory) == 0.02, "the running job's tools see the new budget"

    sdk.resume.set()    # the stream's next paid call is denied in the tool layer
    done = await _wait_for(_terminal, "the end of the job", http, job_id)
    assert done["status"] == "completed", "the job still finishes with what exists"
    assert done["cost"]["spent_usd"] == round(0.02 + 0.21, 4), "the denied call cost nothing"
    assert any("budget enforcement denied a fal_avatar call" in warning for warning in done["warnings"])


async def test_spend_past_the_ceiling_stops_the_run(metered):
    """The watchdog sees real spend, so it fires on calls the guard could not stop."""
    http, sdk = metered

    def bypassed(options):
        directory = ledger_dir(options)
        call = job_ledger.begin_call(directory, tool="fal_avatar", provider="fal.ai", inputs={}, estimate_usd=0.0)
        job_ledger.finish_call(directory, call, cost_usd=9.0, success=True)

    sdk.at_mid_run = bypassed
    job_id = (await http.post("/v1/videos", json={
        "prompt": "a runaway", "options": {"max_budget_usd": 1}})).json()["id"]
    done = await _wait_for(_terminal, "the watchdog", http, job_id)
    assert done["status"] == "failed" and done["error"]["code"] == "budget_exhausted"
    assert done["cost"]["spent_usd"] >= 9.0, "the money stays on the record"
    await asyncio.wait_for(sdk.stopped.wait(), 5)


async def test_cancel_mid_run_stops_the_agent_and_keeps_the_ledger(metered):
    http, sdk = metered
    job_id = (await http.post("/v1/videos", json={
        "prompt": "cancelled midway", "options": {"max_budget_usd": 5}})).json()["id"]
    await asyncio.wait_for(sdk.mid_run.wait(), 5)
    await http.delete(f"/v1/jobs/{job_id}")
    data = await _wait_for(_terminal, "the cancel", http, job_id)
    assert data["status"] == "cancelled"
    await asyncio.wait_for(sdk.stopped.wait(), 5)   # the session stops spending with the job
    items = {line["item"]: line["usd"] for line in data["cost"]["lines"]}
    assert items.get("fal_elevenlabs_tts eleven-v3 (vo.mp3)") == 0.02, "money spent before the cancel stays on the record"
    assert data["cost"]["spent_usd"] >= 0.02
