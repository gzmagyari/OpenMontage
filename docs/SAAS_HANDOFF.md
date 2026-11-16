# SaaS Handoff — everything the SaaS repo's agent needs to know about this API

Audience: the agent/team building the photorealify SaaS backend in the **other** repository.
This file is the institutional knowledge of the fork — read it together with
[`docs/API.md`](API.md) (the endpoint reference with curl examples). Everything below was
measured on real paid runs in November 2026, not estimated.

## 1. What this repository is

A **dedicated fork of OpenMontage** turned into an async video-generator API for
photorealify.com (AI-influencer TikTok ads). Division of labor:

- **This repo** owns: video production, the HTTP job API, Docker image, cost accounting per job.
- **Your repo (the SaaS)** owns: users, credits, moderation, payment, per-customer settings,
  and any outer queue. You talk to this service with one bearer token (`OPENMONTAGE_API_TOKEN`).

The critical architectural fact: **there is no deterministic video pipeline here. Each video job
runs one unattended Claude agent session** (Claude Agent SDK, model `claude-haiku-5-5`) inside
this repo. The agent reads the repo's own playbooks (`AGENT_GUIDE.md`, `pipeline_defs/`,
`.agents/skills/`), picks tools, reviews its own takes frame-by-frame, fixes its own mistakes,
and writes a `RUN_REPORT.md`. Your prompt is not a template fill-in — it is a creative brief
handed to a competent director. Consequences for you:

- Results vary slightly between runs (different voice, slightly different shot choices) unless
  you pin things in the prompt (see §4).
- The agent **degrades gracefully**: if the budget can't cover the ideal plan, it ships the best
  video it can instead of failing.
- The status poll returns the agent's **full chat stream** (see §5) — you can show customers a
  live "the director is working" feed.

## 2. The custom model: `h3max-acting` (our main differentiator)

Stock avatar APIs gave either good acting or good lips, not both. We built a **composite** into
`tools/avatar/fal_avatar.py` called **`h3max-acting`** — it is the tool's default and the
production brief pins it for every on-camera talking shot:

1. **Plate pass** — MiniMax **H3 Max image-to-video** (`minimax/h3-max/image-to-video`,
   $0.035/s): free-form prompted *acting* (gestures, emotion, camera feel) from one portrait
   photo, conditioned on the real TTS audio so the performance matches the speech rhythm.
2. **Lip pass** — MiniMax **H3 Max lip-sync video-to-video**
   (`minimax/h3-max/lip-sync/video-to-video`, $0.05/s, 768P cap): replaces the mouth with
   frame-accurate lip-sync to that same audio.
3. Verbatim re-mux of the original TTS audio onto the result.

**≈ $0.085 per second of talking footage.** Alternatives the tool also supports (kept as
fallbacks, don't default to them): `kling-v2-standard` $0.0562/s, `veed-fabric-1.0` $0.08/s,
`kling-v2-pro` $0.115/s, `omnihuman-1.5` $0.16/s.

Hard-won production rule (now baked into the fal-avatar skill): the lip-sync pass **rejects
videos shorter than 5 s** (fal 422). Any spoken line under ~4.5 s of audio is tail-padded to 5 s
before the call and trimmed after. The agent knows this; you don't need to do anything.

Voice side: TTS is **fal ElevenLabs** (`fal_elevenlabs_tts`, `model_id: eleven-v3`), and the
brief locks the agent to **one voice for the whole video**.

## 3. Exact costs (measured, per ~15 s video)

| Item | Cost | Notes |
|---|---|---|
| Talking footage (`h3max-acting`) | $0.085/s of take | e.g. a 10 s take = $0.85 |
| TTS (fal ElevenLabs eleven-v3) | ~$0.02 | whole script |
| Music (ElevenLabs via fal) | ~$0.60 | only when `options.music` is true and the agent buys a track |
| Claude agent session (Haiku 5.5) | $0.23–0.48 | exact, reported per job |
| **All-in total** | **$1.35–1.70** | validated runs: $1.3484 (host) and $1.6989 (Docker) |

Wall time: **7–9 minutes** per video (50–60 agent turns). Jobs are I/O-bound (waiting on fal),
so ~10 parallel jobs per container is realistic (`AGENT_CONCURRENCY=10` limiter; verified with a
10-parallel test). Remotion rendering (captions/compose) is the only CPU-heavy step and is
separately limited.

How costs reach you: `job.cost.spent_usd` + `job.cost.lines[]`. The Claude line is **exact**
(from the SDK result message). Generation lines come from the job's tool ledger, written by
each paid tool call, and are flagged `estimated: true` — in validation the estimates
matched fal's actuals exactly. Budget controls (all server-enforced): `options.max_budget_usd` caps generation
spend per job (default `DEFAULT_MAX_BUDGET_USD=10`) - the tool layer denies paid calls the
remaining budget cannot cover and the agent exports the best cut from what exists, so send a
budget derived from what the customer can actually pay, on EVERY submit and revise.
`POST /v1/jobs/{id}/budget` re-aims a running job (lower it to the current spend to make the run
wrap up - the "customer ran out of credits" stop). `AGENT_MAX_CLAUDE_USD` (default $3) caps the
Claude-loop spend via the SDK. The cost ledger is live during the run and is kept on failed,
cancelled and timed-out jobs, so bill off `job.cost` actuals plus your margin.

## 4. How to prompt the OpenMontage agent (what your SaaS should send)

**You do NOT need to ask for the custom model.** The server wraps your prompt in a production
brief (`engine/agent/brief.py`) whose locked rules already force: read `AGENT_GUIDE.md` first,
one ElevenLabs voice throughout, **`fal_avatar` with `model: "h3max-acting"` for every on-camera
talking shot**, a muted-autoplay visual hook in second one, BT.709 color, captions/music per
options, the budget with degrade-gracefully semantics, and a cost log. Fighting or repeating
these in your prompt just wastes tokens.

What your prompt SHOULD contain (the creative brief):

- The product/offer, the target viewer, the call to action, required on-screen text or claims.
- The video concept/structure if you have one (hook idea, beats, ending). TikTok lessons from
  our real posts: viewers scroll in 1–4 s, so the **first frame must work muted**; 10–15 s
  beats 30 s; loop-friendly endings help; spoken-intro "ad arc" videos die.
- **Voice pinning (important for per-customer consistency):** the generic brief lets the agent
  pick any fitting voice — across runs it chose "Jessica", then "Rachel". If a customer persona
  must sound the same in every video, say so in the prompt:
  `Use the ElevenLabs voice "Jessica" for all speech.` This is deliberately YOUR layer's job.
- References via `references` (upload once to `POST /v1/assets`, or multipart, or the draft
  flow). Roles the API understands: `presenter`, `person`, `product`, `logo`, `style`,
  `screenshot`, `before`, `after`, `background`, `voice`, `music`, `other`. The presenter
  portrait is what `h3max-acting` animates — a sharp, well-lit, front-facing 9:16-croppable
  photo gives the best takes.

Options you can set per job: `duration_seconds` (10–60), `language`, `max_budget_usd`,
`captions` (bool), `music` (bool).

## 5. The API in one breath

(Full curl examples in `docs/API.md`; live Swagger at `http://<host>:8710/docs` — port 8710 on
purpose, container listens on 8080 internally.)

- `POST /v1/videos` — prompt + references + options → `202` job. JSON or multipart.
  `"draft": true` creates **without starting**; then `PATCH /v1/jobs/{id}` (edit prompt/options/
  references), `POST /v1/jobs/{id}/files` (upload files onto the job), `POST /v1/jobs/{id}/start`
  (validate + queue; 422 if no prompt; idempotent once started). Drafts expose their editable
  `request` in the poll, hold no queue slot, and expire after `DRAFT_TTL_HOURS` (24) unedited.
- `GET /v1/jobs/{id}` — status, `stage` (voiceover → talking shots → captions → audio mix →
  render), `progress`, cost ledger, warnings, `error {code, message, retryable}`, `result`, and
  **`transcript`: the agent chat stream**. Page it with `?transcript_offset=<last next_offset>`;
  `transcript_limit=0` omits it. Event kinds: `session`, `assistant` (the agent talking),
  `tool` (name + clipped input), `tool_result` (clipped), `ratelimit`, `result` (cost summary),
  `system`. This is what lets your UI stream "what the director is doing" live.
- `GET /v1/jobs/{id}/files` / `/files/{name}` — the deliverable MP4 (`result.video.name`),
  `RUN_REPORT.md` (agent's own production report — good customer-facing material),
  `journal.jsonl`, raw `transcript.jsonl`.
- `POST /v1/jobs/{id}/revise` — feedback on a **completed** job → new job that **resumes the
  same agent session** in the same project: existing paid assets are reused, only what the
  feedback touches is regenerated. Cheap revisions (often < $0.50 if no new footage is bought).
  Revised exports get a new file name; the newest MP4 is the result.
- `POST /v1/assets` — upload once (SHA-256 de-duplicated), reference by `asset_id` anywhere.
- `GET /healthz`, `/readyz` (checks ffmpeg/node/keys), `/v1/capabilities`, `/v1/stats`.
- Errors worth handling: `429 queue_full` (+ `Retry-After`) on submit/start; job `error.code`
  values include `agent_failed`, `agent_timeout` (wall-time kill, default 45 min),
  `agent_auth`/`agent_unavailable` (server misconfig — alert, don't bill), `parent_not_found`/
  `parent_expired` on revise. `Idempotency-Key` (header or body) makes submits retry-safe.
  Jobs survive container restarts (they resume); finished jobs expire after `JOB_TTL_HOURS`
  (72) — **download the MP4 before TTL**, your repo should own long-term storage.

## 6. Operations

- Image: `make docker-build` → `openmontage-api` (~3.9 GB, CPU-only). Run:
  `make docker-run` (= `docker run --init --shm-size=2g -p 8710:8080 --env-file .env
  -v openmontage-data:/data openmontage-api`). **One uvicorn worker per container** by design;
  scale out with more containers.
- Required env: `ANTHROPIC_API_KEY`, `FAL_KEY`, `OPENMONTAGE_API_TOKEN`. Tuning:
  `OPENMONTAGE_AGENT_MODEL` (default `claude-haiku-5-5` — don't upgrade it casually, Haiku is
  validated and 5–10x cheaper), `AGENT_MAX_TURNS` (500), `AGENT_WALL_TIME_MINUTES` (45),
  `AGENT_MAX_CLAUDE_USD` (3), `AGENT_CONCURRENCY` (10), `MAX_ACTIVE_JOBS`, `MAX_QUEUED_JOBS`,
  `DEFAULT_MAX_BUDGET_USD`, `JOB_TTL_HOURS`, `DRAFT_TTL_HOURS`, `MAX_UPLOAD_MB` (100).
- Everything the service writes lives on the `/data` volume; the code tree is read-only.

## 7. Gotchas and open items (so you don't rediscover them)

- **Voice pinning is your job** (§4) — the brief deliberately doesn't hard-code a voice.
- Whisper occasionally mishears brand names in captions ("Photo Realify"); if the brand must be
  spelled exactly, say so in the prompt ("captions must spell it PhotoRealify").
- `duration_seconds` is a target, not a contract — the agent lands close (asked 15, delivered
  13.6) and favors a tight edit over padding.
- Seedance/Veo refuse photoreal human faces; that's why the stack is MiniMax H3 Max + Kling.
  Don't ask the agent to use Veo for the presenter.
- The chat stream is plain-text internal detail — if you surface it to customers verbatim,
  consider filtering `tool`/`tool_result` events and showing only `assistant` text.
- Content moderation happens in YOUR layer before submit; this API generates what it's asked.
