"""Draft-job lifecycle: create unstarted -> upload -> patch -> start; plus the Swagger contract."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from engine.jobs.models import utcnow

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


async def test_draft_create_upload_patch_start_complete(client, fake_runners):
    # Create with nothing but the draft flag: no prompt needed yet.
    r = await client.post("/v1/videos", json={"draft": True})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "draft" and job["stage"] == "draft"
    assert job["request"]["prompt"] is None  # drafts expose their editable request

    # Upload a reference straight onto the job.
    up = await client.post(f"/v1/jobs/{job['id']}/files",
                           data={"file_roles": '["presenter"]'},
                           files=[("files", ("mia.jpg", b"jpeg-bytes", "image/jpeg"))])
    assert up.status_code == 200, up.text
    refs = up.json()["request"]["references"]
    assert len(refs) == 1 and refs[0]["role"] == "presenter" and refs[0]["asset_id"].startswith("ast_")

    # Starting before a prompt is set must fail with a clear 422.
    r = await client.post(f"/v1/jobs/{job['id']}/start")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"

    # Set the prompt and options.
    r = await client.patch(f"/v1/jobs/{job['id']}",
                           json={"prompt": "Present our app", "options": {"duration_seconds": 15}})
    assert r.status_code == 200
    assert r.json()["request"]["prompt"] == "Present our app"
    assert r.json()["request"]["options"]["duration_seconds"] == 15
    assert r.json()["request"]["references"] == refs  # patch without references keeps them

    # Start -> runs like any submitted job.
    r = await client.post(f"/v1/jobs/{job['id']}/start")
    assert r.status_code == 202
    assert r.json()["status"] in {"queued", "running"}
    # A retried start is a harmless no-op.
    again = await client.post(f"/v1/jobs/{job['id']}/start")
    assert again.status_code == 200 and again.json()["id"] == job["id"]

    fake_runners.gate(job["id"]).set()
    done = await _wait_status(client, job["id"], {"completed"})
    assert "request" not in done  # only drafts expose the request
    stored = client.app.state.store.get(job["id"])
    assert stored.request["references"][0]["asset_id"] == refs[0]["asset_id"]


async def test_patch_and_upload_rejected_once_started(client, fake_runners):
    job = (await client.post("/v1/videos", json=VIDEO)).json()
    r = await client.patch(f"/v1/jobs/{job['id']}", json={"prompt": "Different prompt now"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_a_draft"
    r = await client.post(f"/v1/jobs/{job['id']}/files",
                          files=[("files", ("x.jpg", b"j", "image/jpeg"))])
    assert r.status_code == 409
    fake_runners.gate(job["id"]).set()
    await _wait_status(client, job["id"], {"completed"})


async def test_patch_unknown_asset_rejected(client):
    job = (await client.post("/v1/videos", json={"draft": True})).json()
    r = await client.patch(f"/v1/jobs/{job['id']}",
                           json={"references": [{"asset_id": "ast_missing"}]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_asset"


async def test_drafts_hold_no_queue_slot(client, fake_runners):
    # max_active=3, max_queued=2: six drafts must all be admitted...
    drafts = []
    for _ in range(6):
        r = await client.post("/v1/videos", json={**VIDEO, "draft": True})
        assert r.status_code == 202
        drafts.append(r.json()["id"])
    # ...and normal submissions still fill the real queue to its limits.
    ids = [(await client.post("/v1/videos", json=VIDEO)).json()["id"] for _ in range(5)]
    full = await client.post("/v1/videos", json=VIDEO)
    assert full.status_code == 429
    # Admission applies to a draft only when it starts.
    r = await client.post(f"/v1/jobs/{drafts[0]}/start")
    assert r.status_code == 429 and r.json()["error"]["code"] == "queue_full"
    assert (await client.get(f"/v1/jobs/{drafts[0]}")).json()["status"] == "draft"  # unharmed
    for i in ids:
        fake_runners.gate(i).set()
    for i in ids:
        await _wait_status(client, i, {"completed"})


async def test_draft_cancel_and_no_restart_pickup(client):
    job = (await client.post("/v1/videos", json={**VIDEO, "draft": True})).json()
    cancelled = (await client.delete(f"/v1/jobs/{job['id']}")).json()
    assert cancelled["status"] == "cancelled"
    r = await client.post(f"/v1/jobs/{job['id']}/start")
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_startable"


async def test_abandoned_draft_expires(client):
    job = (await client.post("/v1/videos", json={**VIDEO, "draft": True})).json()
    manager = client.app.state.manager
    stored = client.app.state.store.get(job["id"])
    stored.updated_at = utcnow() - timedelta(hours=25)
    assert manager.cleanup_expired() == 1
    assert (await client.get(f"/v1/jobs/{job['id']}")).status_code == 404


async def test_multipart_draft_create(client):
    r = await client.post("/v1/videos",
                          data={"draft": "true", "file_roles": '["before"]'},
                          files=[("files", ("before.jpg", b"jpeg-1", "image/jpeg"))])
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] == "draft"
    assert job["request"]["references"][0]["role"] == "before"


async def test_openapi_contract(client):
    schema = (await client.get("/openapi.json")).json()
    # The bearer token is a declared security scheme (Authorize button in /docs).
    assert "BearerToken" in schema["components"]["securitySchemes"]
    # POST /v1/videos documents both bodies instead of an opaque Request.
    body = schema["paths"]["/v1/videos"]["post"]["requestBody"]["content"]
    assert "application/json" in body and "multipart/form-data" in body
    assert "prompt" in body["multipart/form-data"]["schema"]["properties"]
    # The draft endpoints are present.
    for path in ("/v1/jobs/{job_id}/start", "/v1/jobs/{job_id}/files"):
        assert path in schema["paths"], path
    assert "patch" in schema["paths"]["/v1/jobs/{job_id}"]
