# OpenMontage Video API

A thin, async, job-based HTTP layer on top of OpenMontage. A SaaS submits a prompt (plus optional
reference files), polls the job, and downloads the finished files. Videos are produced by
OpenMontage's standard agent-driven pipelines (`AGENT_GUIDE.md`, `pipeline_defs/`, `skills/`,
`tools/`) - the API deliberately adds no production logic of its own.

> **Status:** implemented and tested end to end. Each video job runs one unattended agent session
> (Claude Agent SDK, `engine/agent/`) that follows the stock `AGENT_GUIDE.md` routing inside the
> repo, works in `projects/<job_id>/`, and is journaled message by message - the poll response
> returns that chat stream. Requires `ANTHROPIC_API_KEY`, `FAL_KEY`, and Node >= 18 on the host.

## Running

```bash
make api-install          # pip install -r requirements-api.txt
make api                  # uvicorn api.main:app --port 8710 --workers 1
make api-test             # pytest tests/api tests/engine (no network)
```

The default port is **8710** (deliberately uncommon; 8080 collides with too much local tooling).
Override it with `make api API_PORT=9000` / `make docker-run API_PORT=9000`.

One uvicorn worker per container on purpose: jobs run as asyncio tasks in-process and limits are
process-local. Scale out with more containers; the service answers `429 queue_full` when saturated.

Interactive API docs are served at **`http://localhost:8710/docs`** (Swagger UI, with an
Authorize button for the bearer token), plus `/redoc` and the raw schema at `/openapi.json`.

Configuration is env-driven (see `.env.example`): `FAL_KEY`, `ANTHROPIC_API_KEY`,
`OPENMONTAGE_API_TOKEN` (bearer auth), `MAX_ACTIVE_JOBS`, `MAX_QUEUED_JOBS`,
`DEFAULT_MAX_BUDGET_USD`, `JOB_TTL_HOURS`, `OPENMONTAGE_ENABLE_TOOL_API`.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/videos` | Prompt + references + options -> `202` with a job. JSON, or multipart with `files` (+ `file_roles`). `"draft": true` creates without starting. |
| `PATCH /v1/jobs/{id}` | Edit a **draft**: `prompt`, `options`, `references` (list replaced wholesale). 409 once started. |
| `POST /v1/jobs/{id}/files` | Upload reference files straight onto a **draft** (multipart `files` + `file_roles`); appended to its references. |
| `POST /v1/jobs/{id}/start` | Validate the assembled draft and queue it (`draft -> queued`). Idempotent once started. |
| `GET /v1/jobs` | List jobs (filter by `status`, `kind`). |
| `GET /v1/jobs/{id}` | Poll: status, stage, progress, cost, warnings, error, result, and the agent chat stream (`transcript.events`; page with `?transcript_offset=<next_offset>`, `transcript_limit=0` omits it). |
| `GET /v1/jobs/{id}/files` / `/files/{name}` | List / download deliverables (the MP4s, `RUN_REPORT.md`, `journal.jsonl`, raw `transcript.jsonl`). |
| `POST /v1/jobs/{id}/budget` | Re-aim a queued or RUNNING job's paid-generation budget (`{"max_budget_usd": N}`). The server holds the agent to it: a PreToolUse hook denies paid calls the remaining budget cannot cover, so lowering it to the current spend makes the run stop buying and export the best cut. 409 `not_adjustable` on drafts (PATCH options instead) and terminal jobs. |
| `DELETE /v1/jobs/{id}` | Cancel (queued or running). |
| `POST /v1/jobs/{id}/revise` | Feedback on a completed video -> revision job: the agent session is *resumed* with the feedback, in the same project, reusing every asset that still fits. |
| `POST /v1/assets` / `GET /v1/assets/{id}[/content]` | Upload once, reference by `asset_id` (de-duplicated by content). |
| `GET /healthz`, `GET /readyz`, `GET /v1/capabilities`, `GET /v1/stats` | Health and introspection. |
| `GET /v1/tools`, `POST /v1/tools/{name}/runs` | Internal: run one registry tool as a job (`OPENMONTAGE_ENABLE_TOOL_API=1`; code-executing tools are deny-listed; file inputs are `asset:<id>` strings). |

Job lifecycle: `[draft ->] queued -> running -> completed | failed | cancelled`, with `stage`, `stage_detail`,
`progress`, a cost ledger (`cost.lines`, `cost.spent_usd`) and a structured `error`
(`code`, `message`, `retryable`). The cost ledger is LIVE: while the job runs it carries the
Claude-loop estimate (metered from per-message token usage, replaced by the exact SDK figure at
the end) plus every `cost_usd` the paid tools print, parsed server-side from their output - so a
poll shows real spend as it happens, and a failed, cancelled or timed-out job keeps everything
spent up to that moment. `options.max_budget_usd` is enforced, not advisory: paid calls past it
are denied (the agent is told to export what exists), with a watchdog failing the job
(`budget_exhausted`, not retryable) if spend somehow passes 1.5x the budget. `Idempotency-Key` (header or body) makes submits safe to retry.
Interrupted jobs resume after a restart. Finished jobs expire after `JOB_TTL_HOURS`; drafts that
are never started expire `DRAFT_TTL_HOURS` (default 24) after their last edit.

## Integrating (what a SaaS backend calls)

**1. Submit** - JSON for prompt-only, multipart when sending reference images:

```bash
curl -X POST "$API/v1/videos" -H "Authorization: Bearer $TOKEN" \
  -F 'prompt=UGC-style TikTok ad for example.com - a friendly presenter shows the before/after and ends with: your first one is free.' \
  -F 'options={"duration_seconds": 15, "max_budget_usd": 5}' \
  -F 'file_roles=["presenter","before","after"]' \
  -F files=@mia.jpg -F files=@before.jpg -F files=@after.jpg
# -> 202 {"id": "job_...", "status": "running", ...}
```

**1b. Or build the job up as a draft** - create empty, attach and edit at your own pace, then
start. Nothing runs (and nothing is spent) until the start call:

```bash
JOB_ID=$(curl -s -X POST "$API/v1/videos" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"draft": true}' | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

# upload reference files straight onto the job (repeatable; appended to its references)
curl -X POST "$API/v1/jobs/$JOB_ID/files" -H "Authorization: Bearer $TOKEN" \
  -F 'file_roles=["presenter"]' -F files=@mia.jpg

# set / change the prompt and options any number of times
curl -X PATCH "$API/v1/jobs/$JOB_ID" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "UGC-style TikTok ad ...", "options": {"duration_seconds": 15, "max_budget_usd": 5}}'

# GET /v1/jobs/{id} shows a draft's current request (prompt, options, references) for review

curl -X POST "$API/v1/jobs/$JOB_ID/start" -H "Authorization: Bearer $TOKEN"   # -> 202, queued
```

A draft start is validated like a direct submit (missing prompt -> 422 `invalid_request`), and
retrying the start call on an already-started job is a harmless 200.

**2. Poll** - one call returns status, stage, spend, and the agent's chat stream. Pass the last
`next_offset` back to receive only new events (`transcript_limit=0` to skip the stream):

```python
import httpx, time

offset = 0
while True:
    job = httpx.get(f"{API}/v1/jobs/{job_id}", headers=auth,
                    params={"transcript_offset": offset}).json()
    for event in job.get("transcript", {}).get("events", []):
        print(event["kind"], event.get("text") or event.get("input") or "")   # live agent feed
    offset = job.get("transcript", {}).get("next_offset", offset)
    if job["status"] in ("completed", "failed", "cancelled"):
        break
    time.sleep(5)
```

**3. Download** - `job["result"]["video"]["name"]` is the deliverable; `GET /v1/jobs/{id}/files`
lists it alongside `RUN_REPORT.md` (the agent's own production report) and the transcripts:

```bash
NAME=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/v1/jobs/$JOB_ID?transcript_limit=0" \
       | python -c "import sys, json; print(json.load(sys.stdin)['result']['video']['name'])")
curl -H "Authorization: Bearer $TOKEN" -o final.mp4 "$API/v1/jobs/$JOB_ID/files/$NAME"
```

**4. Revise** - feedback resumes the SAME agent session in the same project (assets are reused,
only what the feedback touches is regenerated):

```bash
curl -X POST "$API/v1/jobs/$JOB_ID/revise" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"feedback": "Make the hook text bigger and the music quieter."}'
# -> 202 with a new job id; poll it the same way
```

Costs land in `job["cost"]`: `spent_usd` plus per-item `lines` (the Claude agent spend is exact;
generation lines come from the agent's own cost log). A 15 s presenter video measures about
$1.50 all-in and ~9 minutes wall time on Haiku 5.5.

## Docker

```bash
make docker-build
make docker-run           # --init --shm-size=2g, /data volume for jobs and uploads
```

## Design notes

- `engine/jobs/` - disk-backed job store (atomic writes), FIFO resource limiters that queue
  (never fail) jobs, an asyncio job manager with admission control, cancel, restart recovery and
  TTL cleanup.
- `engine/agent/` - the `VIDEO` / `REVISE` runners: `brief.py` renders the unattended brief
  (prompt + references + the locked production rules + the generation budget with
  degrade-gracefully semantics), `session.py` runs one Claude Agent SDK session (scrubbed env,
  auth self-check, per-message journal, cost from the result message), `runner.py` maps tool calls
  to job stages live, persists the session id for restart-resume, and collects `exports/` +
  `RUN_REPORT.md` into job files. Agent model/limits: `OPENMONTAGE_AGENT_MODEL`
  (default `claude-haiku-5-5`), `AGENT_MAX_TURNS`, `AGENT_WALL_TIME_MINUTES`,
  `AGENT_MAX_CLAUDE_USD`, `AGENT_CONCURRENCY`. The generation budget per job is
  `options.max_budget_usd` (default `DEFAULT_MAX_BUDGET_USD`).
- `engine/runners.py` - `TOOL_RUN` wraps one registry tool (internal API).
- `engine/assets.py` - uploaded references, SHA-256 de-duplicated, with an SSRF guard for URL
  ingestion.
