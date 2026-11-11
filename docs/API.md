# OpenMontage Video API

A thin, async, job-based HTTP layer on top of OpenMontage. A SaaS submits a prompt (plus optional
reference files), polls the job, and downloads the finished files. Videos are produced by
OpenMontage's standard agent-driven pipelines (`AGENT_GUIDE.md`, `pipeline_defs/`, `skills/`,
`tools/`) - the API deliberately adds no production logic of its own.

> **Status:** implemented and tested end to end. Each video job runs one unattended agent session
> (Claude Agent SDK, `engine/agent/`) that follows the stock `AGENT_GUIDE.md` routing inside the
> repo, works in `projects/<job_id>/`, and is journaled message by message - the poll response
> returns that chat stream. Requires `ANTHROPIC_API_KEY`, `FAL_KEY`, and Node >= 18 on the host,
> and Linux with bubblewrap for the agent boxes (the Docker image has it; see "Agent boxes").

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
| `POST /v1/jobs/{id}/budget` | Re-aim a queued or RUNNING job's paid-generation budget (`{"max_budget_usd": N}`). The server holds the agent to it: every tool call checks it within seconds, and paid calls the remaining budget cannot cover are denied, so lowering it to the current spend makes the run stop buying and export the best cut. 409 `not_adjustable` on drafts (PATCH options instead) and terminal jobs. |
| `DELETE /v1/jobs/{id}` | Cancel (queued or running). With `?purge=true`: delete a finished job now, with its project and the finished jobs built on it (409 `still_active` while one runs). |
| `POST /v1/jobs/{id}/stop` | Stop a running video job and keep it: it ends `cancelled` with `stopped: true` and error code `stopped`, with its project, agent session and cost so far kept until it is resumed or purged (never expired). A job not started yet is just cancelled. |
| `POST /v1/jobs/{id}/resume` | Continue a stopped job in a new job of kind `resume`: the same project and (forked) agent session, reusing everything it made. `options.max_budget_usd` is this run's own budget; its ledger holds only what it adds. 409 `not_resumable` unless the job was stopped; an active resume is returned instead of a second one. |
| `POST /v1/jobs/{id}/revise` | Feedback on a completed video -> revision job: the agent session is *resumed* with the feedback, in the same project, reusing every asset that still fits. |
| `POST /v1/assets` / `GET /v1/assets/{id}[/content]` | Upload once, reference by `asset_id` (de-duplicated by content). |
| `GET /healthz`, `GET /readyz`, `GET /v1/capabilities`, `GET /v1/stats` | Health and introspection. |
| `GET /v1/tools`, `POST /v1/tools/{name}/runs` | Internal: run one registry tool as a job (`OPENMONTAGE_ENABLE_TOOL_API=1`; code-executing tools are deny-listed; file inputs are `asset:<id>` strings). |

Job lifecycle: `[draft ->] queued -> running -> completed | failed | cancelled`, with `stage`, `stage_detail`,
`progress`, a cost ledger (`cost.lines`, `cost.spent_usd`) and a structured `error`
(`code`, `message`, `retryable`). The cost ledger is LIVE: while the job runs it carries the
Claude-loop estimate (the higher of the session's own per-message token usage and the box
gateway's meter; it only moves up while the job runs, and the exact SDK figure replaces it at
the end) plus what every paid tool call recorded in the job's tool ledger (a call still running
counts at its estimate, replaced by its real cost when it finishes; a successful call that reports
no cost counts at its quote) - so a poll shows real spend as it happens, and a failed, cancelled or
timed-out job keeps everything spent up to that moment. A paid tool that has no price for a call
(`PriceQuoteRequired`, e.g. a fal route nobody has priced yet) is refused inside a job, since its
spend could be neither billed nor budgeted; the agent is told to use a priced tool, and the job gets
a warning.
A revision's ledger holds only what that revision spent, Claude included: it forks the parent's
session, so a revision that failed is neither billed to the next one nor in its history. `options.max_budget_usd`
is enforced, not advisory: every tool's `execute()` checks it, so paid calls past it are denied
however the agent invokes the tool (the agent is told to export what exists), with a watchdog failing the job
(`budget_exhausted`, not retryable) if spend somehow passes 1.5x the budget. A session that ends without a
video is resumed once before the job fails with `no_video`, and a revision must export a new file. `Idempotency-Key` (header or body) makes submits safe to retry.
Interrupted jobs resume after a restart. Finished jobs expire after `JOB_TTL_HOURS`; drafts that
are never started expire `DRAFT_TTL_HOURS` (default 24) after their last edit. A stopped job never
expires until it is resumed or purged, and a job is kept while a revision or resume built on it is
kept, so the project the newest run works in stays.

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

`options.style` chooses how the video speaks about the product. `"promote"` (the default) is a
direct ad: it ends on a call to action. `"mention"` presents and praises the product just as
fully, and never pushes the viewer to act: no "click", "buy", "go to" or "link in bio", no web
address shown as a place to go, no discount or deadline, and no end card, spoken or on screen.
A revision or a resume keeps the style of the video it continues.

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

The stream holds the raw session (`tool` and `tool_result` events carry commands, paths and
tool names: they are for you) and two kinds of event made for your customer's screen:

- `assistant`: the director's own sentences. The brief asks it to keep file, tool and model
  names, commands and costs out of them; filter them once more before you show them.
- `activity`: every step in plain words, from a fixed list of labels (never a tool, model or
  provider name, a file, a command or money):

  ```json
  {"kind": "activity", "id": "t-12", "state": "started", "code": "plan", "label": "Writing the plan"}
  {"kind": "activity", "id": "d-15", "state": "started", "code": "take", "label": "Filming take B", "age": 2}
  {"kind": "activity", "id": "d-15", "state": "done", "code": "take", "label": "Filming take B", "seconds": 21}
  ```

  One step keeps one `id`: replace what you show for it when a new event with that id arrives.
  `state` is `started` (it may come again with a more exact `label`), `done` or `failed` (with
  `seconds`, how long the step took), or `gone` (drop it: it was nothing worth showing, such as
  a second of thinking). A `started` step that began earlier says how many seconds ago in
  `age`, so a running timer can start from the right place. `code` names the kind of step, for
  an icon. A step that does two things names both, in the order it does them ("Writing the
  shot list, checking the edit"). Steps come from the session's own work (from the moment the director starts writing
  a call) and from the generation calls of its background batch, each with its real start and
  end. Poll every second or two for a view that moves.

**3. Download** - `job["result"]["video"]["name"]` is the deliverable; `GET /v1/jobs/{id}/files`
lists it alongside `RUN_REPORT.md` (the agent's own production report) and the transcripts. The
report opens with a `## Summary` section written for your customer: a few plain sentences about
the video, with no file, tool, model or provider name and no cost. Everything after it is for
you, the operator (every paid call with its cost, timings, problems), so show a customer the
summary, not the whole file:

```bash
NAME=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/v1/jobs/$JOB_ID?transcript_limit=0" \
       | python -c "import sys, json; print(json.load(sys.stdin)['result']['video']['name'])")
curl -H "Authorization: Bearer $TOKEN" -o final.mp4 "$API/v1/jobs/$JOB_ID/files/$NAME"
```

A finished job also carries what the video is posted with:

- `result["cover"]`: the cover picture, a file entry like the video's (`cover.jpg`, a full-size
  frame of the video that the director chose with `kit cover`; when it chose none, the frame one
  second in). `null` only when no frame could be read.
- `result["post"]`: `{"title", "description", "hashtags"}` in the video's language, written by
  the director from the script and the product's real facts and following the video's style (a
  `"mention"` video's post text has no call to action either). The title is at most 100
  characters, the description at most 500, and there are at most 8 hashtags, each without its
  `#`. `null` when the director wrote none even after the one follow-up it is given (the job
  then has a warning saying so).

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
$1.50 all-in and ~9 minutes wall time on Haiku 5.5. A revision job's ledger holds only what that
revision spent: it works in the first job's project and keeps logging to the same cost log, so
the entries the log already held when the revision started are not counted again (the baseline
is kept per job, so a revision resumed after a restart keeps its own earlier entries).

## Docker

```bash
make docker-build
make docker-run           # --init --shm-size=2g, /data volume, and the three box options below
```

Every video job's agent runs in its own box (see "Agent boxes" below), which needs three options on
the container:

- `--security-opt seccomp=docker/seccomp-openmontage.json`: Docker's default profile refuses the
  namespace calls bubblewrap needs; this one allows them and still refuses the kernel
  administration calls nothing here uses.
- `--security-opt systempaths=unconfined`: lets a box forbid nested boxes.
- `--security-opt apparmor=unconfined`: on hosts that run AppArmor (Ubuntu), Docker's
  `docker-default` profile forbids `mount` and `pivot_root`, which bubblewrap needs to build a box.
  Hosts without AppArmor (Docker Desktop) ignore it.

The container runs as an ordinary user (uid 10001) with no capabilities, and each box still applies
on top. Without these options, video jobs fail with `agent_unavailable` instead of running unboxed,
and `GET /readyz` answers 503 with `agent_box: false` and the reason under `problems`, so a deploy
can check the box without running anything in the container. After a rebuild, run
`python scripts/agent_box_check.py` in the container (no cost), `python scripts/kit_box_check.py`
(no cost: a small video made with the video kit inside a box), then `scripts/agent_session_smoke.py`
(about $0.01). After a change to the job brief, the guide or the kit, `scripts/kit_authoring_probe.py`
rehearses the director's planning step on real prompts, in a throwaway container: model calls only,
nothing can be bought, and without `--spend` it calls no model at all (see the script).

## Agent boxes

A video job's agent is driven by the customer's prompt, so it never runs next to the server. Each
session's Claude CLI, and everything it starts, runs in a bubblewrap box (`engine/sandbox/`) that has:

- the system and the repository read-only, the job's project folder and a private `/tmp` writable,
  and nothing else of `/data`;
- its own process list (the server's processes and environment are not visible) and no network of
  its own;
- an environment built from a list: no provider key, no API token. A provider key's name is set to
  a placeholder so tools still see their provider as configured.

It reaches the outside through three sockets the API serves for that job only:

- **Claude gateway** (`gateway.py`): the CLI's `ANTHROPIC_BASE_URL`. The box holds a token valid for
  this job while it runs; the gateway accepts only that token, only the Messages API, only the
  job's models (`OPENMONTAGE_AGENT_MODEL` and small `claude-haiku-*` side models), meters usage and
  stops at 1.5x `AGENT_MAX_CLAUDE_USD`, then adds the real key.
- **Paid-tools desk** (`desk.py`): `BaseTool.execute` hands every call of a tool with runtime API or
  HYBRID to the desk, which refuses input URLs that are not on the public internet, holds the call to
  the budget, runs it in a short-lived provider box (the keys and the network, but only this project
  and the agent box's `/tmp`), and writes the ledger in the job folder the box cannot see. Local
  tools run in the agent box and are only noted, so the stage still follows what ran.
- **Web proxy** (`webproxy.py`): the box's `HTTP(S)_PROXY`, for WebFetch and downloads. Public
  addresses on ports 80 and 443 only, checked after the name resolves; a byte budget per job and a
  `web.log` in the job folder.

`OPENMONTAGE_AGENT_SANDBOX=required` is the image's setting; `off` runs the agent unboxed and is for
development hosts and the test suite only.

A video kit render keeps Chrome's temporary files in the box's own memory filesystem (`/dev/shm`),
not in the job folder on disk. Remotion starts Chrome so that its shared memory, the frames it hands
between its own processes, lives in files under its temp folder; on disk that is 7 to 8 GB of writes
for a 30 second video, and a slow disk then sets the pace of the render (the same project: 115 to
139 s on disk, 29 to 31 s in memory, frame for frame the same picture). In memory it is about 1 GB
for each render in progress. The kit uses `/dev/shm` only when it is a memory filesystem with at
least 2 GB free and writes which it was into `renders/kit_render.json` (`chrome_temp`); the rule is
in `remotion-composer/kit/engine/browserTemp.ts`. `KIT_CHROME_TEMP=disk` on the container switches
this off for every job.

## A voice for each video

A video with a spoken script gets one voice, made for its presenter (`tools/audio/voiceover_tts.py`,
`lib/eleven_voices.py`). The director describes the speaker; the tool designs a voice from that
description (ElevenLabs Voice Design), saves it, and speaks every line with Eleven v4 Turbo. Set
`ELEVENLABS_VOICE_API_KEY` to switch it on. Without the key every video is spoken by a stock voice
through fal, chosen by the presenter's gender and age.

- **The voice is kept as long as the video.** It is saved on the ElevenLabs account under the name
  `om-<installation id>-<job id>`, so a revision speaks a changed line in the same voice. The
  cleanup that removes a job after `JOB_TTL_HOURS` removes its voice in its next round (every 15
  minutes); a deleted job's voice goes the same way.
- **What the account limits, and what happens then.** Voices saved at once: a new video that finds
  no slot free removes the oldest voice this installation keeps (never one younger than 20 minutes,
  never a voice it did not make). When no voice can be saved (the month's voice creations are used
  up, no slot can be freed, a save is refused, a line will not be spoken) the call goes down an
  order by itself, and the job gets a warning that says why:
  1. the saved voice, as above;
  2. **the design's own recording.** A design speaks any text of 100 to 1,000 characters in the
     new voice, and a design that saves nothing uses no slot and none of the month's creations
     (ten in a row moved only the character count). So the whole script is given as that text
     and the recording is cut into the lines (`lib/videokit/cut.py`: the transcriber finds the
     words, each cut is moved into the pause between two lines). The voice still fits the
     presenter. It cannot speak again;
  3. a stock voice through fal, chosen by the presenter's gender and age. Also at once when the
     design itself is refused (a description that names a real person), ElevenLabs cannot be
     reached, too few characters are left, or the script is longer than 1,000 characters.

  Every line comes from the same step. A video never has two voices.
- **A revision of a video whose voice is gone** (removed to make room) **or was a recording** keeps
  its spoken words and says so. It does not speak one line in another voice.
- **`VOICEOVER_FIRST_CHOICE=recording`** makes every new video start at step 2: no voice is ever
  saved, so the month's limit and the slots do not matter, at about twice the price a video and
  without changing a spoken line later. Unset, the order starts at the saved voice.
- **Cost.** ElevenLabs reports each spoken line in its own units, and a design counts the
  characters of its text once. The tool adds them up and turns them into dollars at
  `ELEVENLABS_USD_PER_1K_UNITS` (default 0.35). That line of a job's cost is the tool's own figure:
  unlike a fal line, it cannot be checked against the provider's bill call by call. About $0.07 for
  a 30 second script with a saved voice, about $0.15 as a recording (the whole script is the
  design's text). A call that goes down the order costs what its steps cost.
- **Two installations on one account** (a local one and a server) each need their own
  `OPENMONTAGE_INSTANCE_ID`; each then removes only its own voices. They still share the account's
  slots and monthly counts.
- The key under the name `ELEVENLABS_API_KEY` works too, but that name also switches on the direct
  ElevenLabs tools (single lines, music, soundtracks) for every job, on the same allowance.

## Design notes

- `engine/jobs/` - disk-backed job store (atomic writes), FIFO resource limiters that queue
  (never fail) jobs, an asyncio job manager with admission control, cancel, restart recovery and
  TTL cleanup.
- `engine/agent/` - the `VIDEO` / `REVISE` runners: `brief.py` renders the unattended brief
  (prompt + references + the locked production rules + the generation budget with
  degrade-gracefully semantics), `session.py` runs one Claude Agent SDK session (scrubbed env,
  auth self-check, per-message journal, cost from the result message, and one continuous turn: a
  tool allow-list, long Bash limits, background tasks off), `runner.py` maps tool calls to job
  stages live, mirrors the job's tool ledger (`lib/job_ledger.py`, written from
  `tools/base_tool.py`) into the cost lines, persists the session id for restart-resume, keeps
  the session itself in the project on the data volume so revisions and resumes survive a
  redeploy, stops anything a finished session left running, and collects `exports/` + `RUN_REPORT.md` into job files. The SDK version is pinned because it
  decides how sessions behave: after a rebuild or upgrade run `scripts/agent_session_smoke.py`
  in the container (about $0.01, no paid tools). Agent model/limits: `OPENMONTAGE_AGENT_MODEL`
  (default `claude-haiku-5-5`), `AGENT_MAX_TURNS`, `AGENT_WALL_TIME_MINUTES`,
  `AGENT_MAX_CLAUDE_USD`, `AGENT_CONCURRENCY`. The generation budget per job is
  `options.max_budget_usd` (default `DEFAULT_MAX_BUDGET_USD`).
- `engine/runners.py` - `TOOL_RUN` wraps one registry tool (internal API).
- `engine/assets.py` - uploaded references, SHA-256 de-duplicated, with an SSRF guard for URL
  ingestion.
