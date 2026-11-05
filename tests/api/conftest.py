"""Fixtures: an API app wired to fake runners (no providers, no network)."""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
import pytest_asyncio

from engine.config import load_settings


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMONTAGE_DATA_DIR", str(tmp_path / "data"))
    base = load_settings()
    return dataclasses.replace(
        base,
        data_dir=tmp_path / "data",
        projects_dir=tmp_path / "projects",
        api_token="test-token",
        max_active_jobs=3,
        max_queued_jobs=2,
        render_concurrency=1,
        job_ttl_hours=1.0,
        enable_tool_api=False,
    )


class FakeRunners:
    """Runners whose behaviour tests can steer: each job waits on a per-job gate."""

    def __init__(self):
        self.gates: dict[str, asyncio.Event] = {}
        self.started: list[str] = []
        self.render_order: list[str] = []

    def gate(self, job_id: str) -> asyncio.Event:
        return self.gates.setdefault(job_id, asyncio.Event())

    async def video(self, ctx):
        from engine.jobs import JobFailed

        if ctx.job.request.get("prompt") == "FAIL":
            raise JobFailed("provider_refused", "All providers refused", retryable=False)
        self.started.append(ctx.job.id)
        ctx.set_stage("planning", progress=5)
        await self.gate(ctx.job.id).wait()
        ctx.set_stage("rendering", progress=80)
        async with ctx.resource("render", "render"):
            self.render_order.append(ctx.job.id)
            await asyncio.sleep(0.05)
        (ctx.files_dir / "video.mp4").write_bytes(b"fake-mp4")
        ctx.add_cost("fake clip", 1.25, provider="fake")
        return {"files": {"video": {"name": "video.mp4", "size_bytes": 8, "content_type": "video/mp4"}}}

    def mapping(self):
        from engine.jobs import JobKind

        return {JobKind.VIDEO: self.video, JobKind.REVISE: self.video}


@pytest.fixture()
def fake_runners():
    return FakeRunners()


@pytest_asyncio.fixture()
async def client(settings, fake_runners):
    import httpx

    from api.main import create_app

    app = create_app(settings=settings, runners=fake_runners.mapping())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                     headers={"Authorization": "Bearer test-token"}) as http:
            http.app = app  # type: ignore[attr-defined]
            yield http
