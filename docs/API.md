# OpenMontage Video API

A thin, async, job-based HTTP layer on top of OpenMontage. A SaaS submits a prompt (plus optional
reference files), polls the job, and downloads the finished files. Videos are produced by
OpenMontage's standard agent-driven pipelines (`AGENT_GUIDE.md`, `pipeline_defs/`, `skills/`,
`tools/`) - the API deliberately adds no production logic of its own.

> **Status:** the job layer (submit / poll / files / cancel / limits) is implemented and tested.
> The unattended agent runner that executes a pipeline per job is not built yet, so
> `POST /v1/videos` currently answers `501 not_implemented`. The internal tool API works.

## Running

```bash
make api-install          # pip install -r requirements-api.txt
make api                  # uvicorn api.main:app --port 8080 --workers 1
make api-test             # pytest tests/api tests/engine (no network)
```

One uvicorn worker per container on purpose: jobs run as asyncio tasks in-process and limits are
process-local. Scale out with more containers; the service answers `429 queue_full` when saturated.

Configuration is env-driven (see `.env.example`): `FAL_KEY`, `ANTHROPIC_API_KEY`,
`OPENMONTAGE_API_TOKEN` (bearer auth), `MAX_ACTIVE_JOBS`, `MAX_QUEUED_JOBS`,
`DEFAULT_MAX_BUDGET_USD`, `JOB_TTL_HOURS`, `OPENMONTAGE_ENABLE_TOOL_API`.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /v1/videos` | Prompt + references + options -> `202` with a job. JSON, or multipart with `files` (+ `file_roles`). *(501 until the agent runner lands.)* |
| `GET /v1/jobs` | List jobs (filter by `status`, `kind`). |
| `GET /v1/jobs/{id}` | Poll: status, stage, progress, cost, warnings, error, result. |
| `GET /v1/jobs/{id}/files` / `/files/{name}` | List / download deliverables. |
| `DELETE /v1/jobs/{id}` | Cancel (queued or running). |
| `POST /v1/jobs/{id}/revise` | Feedback on a completed video -> revision job. *(501 until the agent runner lands.)* |
| `POST /v1/assets` / `GET /v1/assets/{id}[/content]` | Upload once, reference by `asset_id` (de-duplicated by content). |
| `GET /healthz`, `GET /readyz`, `GET /v1/capabilities`, `GET /v1/stats` | Health and introspection. |
| `GET /v1/tools`, `POST /v1/tools/{name}/runs` | Internal: run one registry tool as a job (`OPENMONTAGE_ENABLE_TOOL_API=1`; code-executing tools are deny-listed; file inputs are `asset:<id>` strings). |

Job lifecycle: `queued -> running -> completed | failed | cancelled`, with `stage`, `stage_detail`,
`progress`, a cost ledger (`cost.lines`, `cost.spent_usd`) and a structured `error`
(`code`, `message`, `retryable`). `Idempotency-Key` (header or body) makes submits safe to retry.
Interrupted jobs resume after a restart. Finished jobs expire after `JOB_TTL_HOURS`.

## Docker

```bash
make docker-build
make docker-run           # --init --shm-size=2g, /data volume for jobs and uploads
```

## Design notes

- `engine/jobs/` - disk-backed job store (atomic writes), FIFO resource limiters that queue
  (never fail) jobs, an asyncio job manager with admission control, cancel, restart recovery and
  TTL cleanup.
- `engine/runners.py` - job runners. `TOOL_RUN` wraps one registry tool; the `VIDEO` / `REVISE`
  runner (an unattended agent following the standard pipelines, with delegated approvals and a hard
  budget cap) is the next milestone.
- `engine/assets.py` - uploaded references, SHA-256 de-duplicated, with an SSRF guard for URL
  ingestion.
