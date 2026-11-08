"""Job API contract: submit -> poll -> files; auth; idempotency; cancel; queueing; 429."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio

VIDEO = {"prompt": "An AI influencer hypes our app", "options": {"duration_seconds": 20}}


async def _wait_status(client, job_id, wanted, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        job = (await client.get(f"/v1/jobs/{job_id}")).json()
        if job["status"] in wanted:
            return job
        assert asyncio.get_running_loop().time() < deadline, f"job stuck in {job['status']}"
        await asyncio.sleep(0.02)


async def test_health_needs_no_auth(client):
    r = await client.get("/healthz", headers={"Authorization": ""})
    assert r.status_code == 200 and r.json()["status"] == "ok"


async def test_ready_checks_the_agent_box_when_boxes_are_required(client, settings, monkeypatch):
    import dataclasses

    from engine.sandbox import box

    r = await client.get("/readyz", headers={"Authorization": ""})
    assert "agent_box" not in r.json()["checks"]  # boxes off (tests, development hosts)

    monkeypatch.setattr(client.app.state, "settings", dataclasses.replace(settings, agent_sandbox="required"))
    monkeypatch.setattr(box, "sandbox_problem", lambda: "bubblewrap cannot create a box")
    r = await client.get("/readyz", headers={"Authorization": ""})
    assert r.status_code == 503
    assert r.json()["checks"]["agent_box"] is False
    assert r.json()["problems"] == {"agent_box": "bubblewrap cannot create a box"}

    monkeypatch.setattr(box, "sandbox_problem", lambda: None)
    r = await client.get("/readyz", headers={"Authorization": ""})
    assert r.json()["checks"]["agent_box"] is True and "problems" not in r.json()


async def test_auth_required(client):
    r = await client.post("/v1/videos", json=VIDEO, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


async def test_submit_poll_complete_and_download(client, fake_runners):
    r = await client.post("/v1/videos", json=VIDEO)
    assert r.status_code == 202
    job = r.json()
    assert job["status"] in {"queued", "running"} and job["id"].startswith("job_")
    assert r.headers["location"] == f"/v1/jobs/{job['id']}"

    fake_runners.gate(job["id"]).set()
    done = await _wait_status(client, job["id"], {"completed"})
    assert done["progress"] == 100
    assert done["cost"]["spent_usd"] == 1.25
    url = done["result"]["files"]["video"]["url"]
    assert url.endswith(f"/v1/jobs/{job['id']}/files/video.mp4")
    blob = await client.get(f"/v1/jobs/{job['id']}/files/video.mp4")
    assert blob.status_code == 200 and blob.content == b"fake-mp4"
    listing = (await client.get(f"/v1/jobs/{job['id']}/files")).json()["files"]
    assert [f["name"] for f in listing] == ["video.mp4"]


async def test_file_name_traversal_rejected(client, fake_runners):
    job = (await client.post("/v1/videos", json=VIDEO)).json()
    r = await client.get(f"/v1/jobs/{job['id']}/files/..%2Fjob.json")
    assert r.status_code in (400, 404)


async def test_idempotency_returns_same_job(client):
    a = await client.post("/v1/videos", json=VIDEO, headers={"Idempotency-Key": "abc"})
    b = await client.post("/v1/videos", json=VIDEO, headers={"Idempotency-Key": "abc"})
    assert a.status_code == 202 and b.status_code == 200
    assert a.json()["id"] == b.json()["id"]


async def test_validation_error_shape(client):
    r = await client.post("/v1/videos", json={"prompt": "x"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


async def test_failed_job_reports_error(client):
    r = await client.post("/v1/videos", json={**VIDEO, "prompt": "FAIL"})
    job = await _wait_status(client, r.json()["id"], {"failed"})
    assert job["error"] == {"code": "provider_refused", "message": "All providers refused", "retryable": False}


async def test_queueing_cancel_and_queue_full(client, fake_runners):
    # max_active=3, max_queued=2
    ids = [(await client.post("/v1/videos", json=VIDEO)).json()["id"] for _ in range(5)]
    jobs = [(await client.get(f"/v1/jobs/{i}")).json() for i in ids]
    assert [j["status"] for j in jobs[:3]] == ["running"] * 3
    assert [j["queue_position"] for j in jobs[3:]] == [1, 2]
    assert "waiting for a job slot (#1 in line)" in jobs[3]["stage_detail"]

    full = await client.post("/v1/videos", json=VIDEO)
    assert full.status_code == 429 and full.headers["retry-after"] == "30"
    assert full.json()["error"]["code"] == "queue_full"

    cancelled = (await client.delete(f"/v1/jobs/{ids[3]}")).json()
    assert cancelled["status"] == "cancelled"
    assert (await client.get(f"/v1/jobs/{ids[4]}")).json()["queue_position"] == 1

    running_cancel = (await client.delete(f"/v1/jobs/{ids[0]}")).json()
    assert running_cancel["id"] == ids[0]
    assert (await _wait_status(client, ids[0], {"cancelled"}))["error"]["code"] == "cancelled"
    # the freed slot starts the next queued job
    await _wait_status(client, ids[4], {"running"})
    for i in ids[1:3] + ids[4:]:
        fake_runners.gate(i).set()
    for i in ids[1:3] + ids[4:]:
        await _wait_status(client, i, {"completed"})


async def test_cancel_before_start_frees_slot(client, fake_runners):
    """A job cancelled before its task ever ran must end cancelled and release its slot."""
    mgr = client.app.state.manager
    ids = [(await client.post("/v1/videos", json=VIDEO)).json()["id"] for _ in range(3)]
    for i in ids:
        await client.delete(f"/v1/jobs/{i}")
    for i in ids:
        assert (await _wait_status(client, i, {"cancelled"}))["error"]["code"] == "cancelled"
    assert mgr.stats()["running"] == 0
    fresh = (await client.post("/v1/videos", json=VIDEO)).json()["id"]
    fake_runners.gate(fresh).set()
    await _wait_status(client, fresh, {"completed"})


async def test_parallel_jobs_never_fail_on_render_waits(client, fake_runners):
    """render capacity is 1: jobs queue for it FIFO and all complete (none fail)."""
    ids = [(await client.post("/v1/videos", json=VIDEO)).json()["id"] for _ in range(3)]
    for i in ids:
        fake_runners.gate(i).set()
    results = [await _wait_status(client, i, {"completed", "failed"}) for i in ids]
    assert all(r["status"] == "completed" for r in results)
    assert sorted(fake_runners.render_order) == sorted(ids)


async def test_revise_requires_completed_parent(client, fake_runners):
    job = (await client.post("/v1/videos", json=VIDEO)).json()
    r = await client.post(f"/v1/jobs/{job['id']}/revise", json={"feedback": "make it shorter"})
    assert r.status_code == 409
    fake_runners.gate(job["id"]).set()
    await _wait_status(client, job["id"], {"completed"})
    r = await client.post(f"/v1/jobs/{job['id']}/revise", json={"feedback": "make it shorter"})
    assert r.status_code == 202 and r.json()["parent_job_id"] == job["id"]


async def test_asset_upload_and_reference(client):
    up = await client.post("/v1/assets", files={"file": ("logo.png", b"\x89PNG fake", "image/png")})
    assert up.status_code == 201
    asset = up.json()
    assert asset["kind"] == "image" and asset["id"].startswith("ast_")
    again = await client.post("/v1/assets", files={"file": ("copy.png", b"\x89PNG fake", "image/png")})
    assert again.json()["id"] == asset["id"]  # de-duplicated by content
    body = {**VIDEO, "references": [{"asset_id": asset["id"], "role": "logo"}]}
    assert (await client.post("/v1/videos", json=body)).status_code == 202
    bad = {**VIDEO, "references": [{"asset_id": "ast_missing"}]}
    assert (await client.post("/v1/videos", json=bad)).json()["error"]["code"] == "unknown_asset"


async def test_multipart_video_request(client):
    files = [("files", ("before.jpg", b"jpeg-bytes-1", "image/jpeg")), ("files", ("after.jpg", b"jpeg-bytes-2", "image/jpeg"))]
    data = {"prompt": "Show our before and after", "file_roles": '["before", "after"]', "options": '{"duration_seconds": 15}'}
    r = await client.post("/v1/videos", data=data, files=files)
    assert r.status_code == 202, r.text


async def test_restart_recovery_requeues_running_jobs(settings, fake_runners):
    import httpx

    from api.main import create_app

    app = create_app(settings=settings, runners=fake_runners.mapping())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                                     headers={"Authorization": "Bearer test-token"}) as http:
            job_id = (await http.post("/v1/videos", json=VIDEO)).json()["id"]
            await _wait_status(http, job_id, {"running"})
    # shutdown mid-job -> next boot resumes it
    app2 = create_app(settings=settings, runners=fake_runners.mapping())
    async with app2.router.lifespan_context(app2):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app2), base_url="http://t",
                                     headers={"Authorization": "Bearer test-token"}) as http:
            fake_runners.gate(job_id).set()
            job = await _wait_status(http, job_id, {"completed"})
            assert job["attempts"] >= 1 and job["result"]["files"]["video"]["name"] == "video.mp4"
