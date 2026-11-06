"""POST /v1/jobs/{id}/budget, and live cost accounting through a fake SDK stream."""

from __future__ import annotations

import asyncio
import json

import pytest
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
    """A stream whose tool results carry real cost_usd output, gated midway so the
    test can observe the LIVE ledger before the run finishes."""

    def __init__(self):
        self.mid_run = asyncio.Event()
        self.resume = asyncio.Event()

    def install(self, monkeypatch):
        fake = self

        async def fake_query(*, prompt, options=None, **_):
            yield SystemMessage(subtype="init", data={
                "session_id": "sess-live", "apiKeySource": "ANTHROPIC_API_KEY",
                "model": options.model, "tools": ["Bash"]})
            yield AssistantMessage(
                content=[TextBlock(text="Buying the voiceover."),
                         ToolUseBlock(id="t1", name="Bash",
                                      input={"command": "python tools/audio/fal_elevenlabs_tts.py --voice J"})],
                model=options.model, usage={"input_tokens": 200_000, "output_tokens": 10_000})
            yield UserMessage(content=[ToolResultBlock(
                tool_use_id="t1", is_error=False,
                content='{"tool": "fal_elevenlabs_tts", "cost_usd": 0.02}')])
            fake.mid_run.set()
            await fake.resume.wait()
            yield AssistantMessage(
                content=[ToolUseBlock(id="t2", name="Bash",
                                      input={"command": "python tools/avatar/fal_avatar.py take1"})],
                model=options.model)
            yield UserMessage(content=[ToolResultBlock(
                tool_use_id="t2", is_error=False,
                content='{"tool": "fal_avatar", "cost_usd": 0.88, "cost_breakdown": {"plate": 0.4, "sync": 0.48}}')])
            import re as _re
            project = _re.search(r"All project work goes in: (.+)", prompt).group(1).strip()
            exports = agent_session.Path(project) / "exports"
            exports.mkdir(parents=True, exist_ok=True)
            (exports / "video.mp4").write_bytes(b"fake-mp4-bytes")
            artifacts = agent_session.Path(project) / "artifacts"
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / "cost_log.json").write_text(json.dumps([
                {"item": "fal_elevenlabs_tts voiceover", "usd": 0.02},
                {"item": "fal_avatar h3max-acting take1", "usd": 0.93},
            ]), encoding="utf-8")
            yield AssistantMessage(content=[TextBlock(text="Exported.")], model=options.model)
            yield ResultMessage(subtype="success", duration_ms=900, duration_api_ms=800,
                                is_error=False, num_turns=4, session_id="sess-live",
                                total_cost_usd=0.21, result="done")

        monkeypatch.setattr(agent_session, "sdk_query", fake_query)
        return fake


async def test_live_ledger_then_cost_log_reconciliation(settings, monkeypatch):
    import dataclasses

    import httpx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("FAL_KEY", "test-fal-key")
    sdk = MeteredSDK().install(monkeypatch)
    cfg = dataclasses.replace(settings, agent_wall_time_minutes=0.2)
    app = create_app(settings=cfg)  # the real agent runners
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Authorization": "Bearer test-token"}) as http:
            job_id = (await http.post("/v1/videos", json={
                "prompt": "a metered run", "options": {"duration_seconds": 15, "max_budget_usd": 5}})).json()["id"]

            await asyncio.wait_for(sdk.mid_run.wait(), 5)
            live = (await http.get(f"/v1/jobs/{job_id}", params={"transcript_limit": 0})).json()
            lines = {line["item"]: line for line in live["cost"]["lines"]}
            assert lines["tts call (reported live)"]["usd"] == 0.02, "tool spend shows while running"
            claude = lines["claude agent"]
            assert claude["estimated"] is True and claude["usd"] == 0.25  # 0.2M in + 10k out at haiku rates
            assert live["cost"]["spent_usd"] == 0.27

            sdk.resume.set()
            for _ in range(300):
                done = (await http.get(f"/v1/jobs/{job_id}", params={"transcript_limit": 0})).json()
                if done["status"] in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.02)
            assert done["status"] == "completed"
            items = [line["item"] for line in done["cost"]["lines"]]
            # The agent's cost log (0.95) covers the observed spend (0.90): it replaces the live lines.
            assert "fal_avatar h3max-acting take1" in items and "tts call (reported live)" not in items
            claude = next(line for line in done["cost"]["lines"] if line["item"] == "claude agent")
            assert claude["estimated"] is False and claude["usd"] == 0.21
            assert done["cost"]["spent_usd"] == round(0.02 + 0.93 + 0.21, 4)
            assert done["warnings"] == []


async def test_cancel_mid_run_keeps_the_live_ledger(settings, monkeypatch):
    import dataclasses

    import httpx

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("FAL_KEY", "test-fal-key")
    sdk = MeteredSDK().install(monkeypatch)   # never resumed: the run hangs mid-stream
    cfg = dataclasses.replace(settings, agent_wall_time_minutes=1)
    app = create_app(settings=cfg)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Authorization": "Bearer test-token"}) as http:
            job_id = (await http.post("/v1/videos", json={
                "prompt": "cancelled midway", "options": {"max_budget_usd": 5}})).json()["id"]
            await asyncio.wait_for(sdk.mid_run.wait(), 5)
            await http.delete(f"/v1/jobs/{job_id}")
            for _ in range(300):
                data = (await http.get(f"/v1/jobs/{job_id}", params={"transcript_limit": 0})).json()
                if data["status"] in ("completed", "failed", "cancelled"):
                    break
                await asyncio.sleep(0.02)
            assert data["status"] == "cancelled"
            items = {line["item"]: line["usd"] for line in data["cost"]["lines"]}
            assert items.get("tts call (reported live)") == 0.02, "money spent before the cancel stays on the record"
            assert data["cost"]["spent_usd"] >= 0.02
