# OpenMontage - Agent Guide

Start here. This is the complete operating guide and agent contract for OpenMontage.

For architecture, key files, and conventions see [`PROJECT_CONTEXT.md`](PROJECT_CONTEXT.md).

## First Interaction — Onboarding

When the user's first message is vague, exploratory, or asks what you can do ("make me a video", "what can you do?", "help me create something", "I want to make content"), read the onboarding skill **before** doing anything else:

**Read:** `skills/meta/onboarding.md`

This skill teaches you to run discovery, classify the user's setup, present capabilities in plain language, and offer starter prompts tailored to their available tools. The goal: get the user from "curious" to "making a video" in under 60 seconds.

**Skip onboarding** when the user arrives with a specific, actionable request (e.g., "Make a 60-second explainer about black holes"). Go directly to Rule Zero.

## Reference Video Entry Point

When the user provides a **video URL or local video file as inspiration** — for example:

- "Can you make a video like this?"
- "I love this YouTube Short. Make me something similar."
- "Use this Reel as a reference."

— do **not** treat this as a generic web-search or prompt-writing request.

This is a first-class workflow in OpenMontage.

### Required behavior

1. **Read:** `skills/meta/video-reference-analyst.md`
2. **Run the reference analysis workflow** using the local analysis tools (`video_analyzer`, transcript extraction, scene detection, frame sampling)
3. **Produce a grounded summary** of what the reference is doing:
   - content
   - pacing
   - structure
   - style
   - what makes it work
4. **Then** run normal capability audit and pipeline selection
5. Present **2-3 differentiated concepts** for the user's version — not a carbon copy

### Important distinction

- **Reference-driven request:** "make me something like this" -> use `video-reference-analyst.md`
- **Source-footage request:** "edit this footage" / "cut this into clips" -> use `source_media_review` and the appropriate footage-led pipeline

If a model misses this distinction, it will often fall back to plain search + guesswork. That is incorrect for OpenMontage.

## Rule Zero — All Production Goes Through a Pipeline

**Every video production request MUST go through the pipeline system. No exceptions.**

When the user asks to make, create, produce, or generate any video content — a trailer, explainer, clip, animation, or any other video — the agent must:

1. **Identify the pipeline.** Match the request to one of the pipelines in `pipeline_defs/`. If unclear, ask the user.
2. **Read the pipeline manifest.** `pipeline_defs/<pipeline>.yaml` — know the stages, tools, and quality gates.
3. **Run preflight.** Discover available tools via the registry. Present the capability menu.
4. **Execute stage by stage.** For EACH stage, read the stage director skill (`skills/pipelines/<pipeline>/<stage>-director.md`) BEFORE doing any work in that stage.
5. **Read Layer 3 skills before calling tools.** Before using any tool with an `agent_skills` field, read the referenced skill in `.agents/skills/`. These contain provider-specific prompting guidance, parameter optimization, and quality techniques that dramatically improve output.

**Do NOT:**
- Write ad-hoc Python scripts to call tools directly
- Skip the pipeline and go straight to API calls
- Generate assets without reading the stage director skill first
- Use a tool without checking its Layer 3 skill for prompting guidance
- Bypass preflight, checkpoints, or review

The intelligence is in the skills, not in improvised code. An agent that reads the director skills and Layer 3 knowledge will produce significantly better output than one that calls tools directly with generic prompts.

## What OpenMontage Is

OpenMontage is an instruction-driven video production system. The AI agent IS the intelligence — it reads instructions (pipeline manifests + stage director skills + meta skills) and drives the pipeline using tools.

```
Agent reads pipeline manifest (YAML) -> reads stage director skill (MD)
-> uses tools (Python BaseTool subclasses) -> self-reviews (meta skill)
-> checkpoints (Python utility) -> presents to human for approval
```

**Python = tools + persistence.** No orchestration logic, creative decisions, review logic, or checkpoint policy in Python code. The agent makes those decisions guided by instructions.

Core loop:

1. Select a pipeline.
2. Run preflight.
3. Discover real tools from the registry.
4. Present the user with concepts, tool plan, production plan, and cost.
5. Execute stage by stage with checkpoints.

## Decision Communication Contract

For any meaningful production decision, the agent must communicate the decision before acting. The user should never have to infer which provider, model, or render path was chosen after the fact.

### Announce Before Execution

Before any paid or consequential generation call, state:

- the exact tool name,
- the provider,
- the model or provider variant,
- the reason it was chosen,
- whether it is a sample or a batch run.

### Ask Before Major Changes

The agent must ask the user before changing any major production choice, including:

- switching provider,
- switching model family or provider variant,
- switching from video-led to still-led treatment,
- switching composition engine when that changes the output character,
- dropping narration, music, or other approved creative elements,
- changing from sample mode to batch mode.

Minor prompt refinements inside an already approved provider/model path do not require separate approval unless they materially change the creative direction.

### Re-log Changed Decisions (Binding)

The `decision_log` is the board's Decisions rail and the run's audit trail. It is **append-only history, not a scratchpad.** When a choice you already logged changes mid-run — the user swaps the voice, you switch provider/model/runtime/music, or a fallback overrides an earlier pick — you MUST **append a new `decision_log` entry** for the new choice, reusing the **same `category` AND the same `subject`** (e.g. `category: "voice_selection"`, `subject: "Narration TTS provider"`), with the superseded option moved into `options_considered` and `rejected_because` noting it was changed.

Editing only a downstream artifact (the `asset_manifest`, a prop) while leaving the old decision in the log is a defect: the board keeps showing the stale choice (e.g. `voice → openai_onyx` after the user moved to Chirp3). The board identifies a decision by its **(category, subject) pair** and renders the latest entry for that pair as current (tagged "revised") — so the fix is to append the new entry with an identical `subject`, never to silently mutate the old one or reword the subject (a reworded subject reads as a different decision and both will show). Keeping distinct decisions in one category (e.g. TTS vs image `provider_selection`) is exactly why the pair, not the category alone, is the key. This applies at every stage, not just `idea`.

### Present Both Composition Runtimes (HARD RULE)

When both Remotion and HyperFrames are available on the machine (check `video_compose.get_info()["render_engines"]`), the agent **MUST present both options to the user** before locking `render_runtime` at the proposal stage. The agent MAY recommend one with rationale — but silently picking a "default" is forbidden even when the pipeline manifest or a director skill suggests one.

The presentation MUST include, for each runtime:

1. A one-sentence plain-language description of what it is best at for **this specific brief**.
2. A one-sentence honest tradeoff (why it might not be the right pick here).
3. The agent's recommendation and the reason, tied to the brief's delivery_promise and visual approach.

Then wait for explicit user approval before advancing. Record the full shortlist — BOTH runtimes plus any "ffmpeg" option that applies — as `options_considered` in the `render_runtime_selection` decision logged in `decision_log`. A decision log entry with only one runtime considered when both were available is a CRITICAL reviewer finding.

Exception: if only one runtime is available on the machine, the agent proceeds with it but MUST say so explicitly ("HyperFrames isn't installed on this machine; I'm proceeding with Remotion. Install HyperFrames if you want the alternative."). The `render_runtime_selection` decision still records the unavailable option as `rejected_because: "runtime not available on this machine"`.

This rule applies to every pipeline that invokes `video_compose` — not just Wave 1. A pipeline's director skill may recommend a runtime, but that recommendation is input to the conversation with the user, not a decision.

### Composition Authoring Mode — Templated vs Atelier

Orthogonal to *runtime* is *authoring mode*: **how** the composition is built. Present it as its own proposal decision and log it in `decision_log` (`category: "composition_mode"`).

- **Templated** — assemble the stock `cut.type` scene-types (`text_card`, `stat_card`, `bar_chart`, …) into the `Explainer`/`CinematicRenderer` compositions. Fast, cheap, reliable — and the reason most videos look alike. Right for batch output, localization variants, quick drafts, and low-stakes internal clips.
- **Atelier** — **hand-author the composition from scratch**: bespoke scenes, a one-off theme, and motion written for this piece, rendered via `composition_mode: "atelier"` (see `video_compose` → `_render_via_atelier`). No reusable creative components; a fresh visual language every time.

**Default to atelier for hero work** — marketing, launches, brand pieces, any single-deliverable explainer that must impress. The deciding rule: *reuse engine knowledge, never creative components.* In atelier mode the stock scene-type catalog, `hyperframes-registry` blocks, fixtures, and finished components are **off-limits** — they are frozen looks that reintroduce sameness. Before building, route through **`skills/meta/taste-direction.md`** to set the design read and taste dials, then **`skills/meta/bespoke-composition.md`**, which sequences: art direction (`visual-style`) → motion principles (Disney 12 via `framer-motion`/`lottie-bodymovin`) → engine mechanics (`remotion-best-practices` + the stock components read *only as a mechanics codex*) → render via the atelier path. Close with a **distinctness review**: *could this be any other product's video? does it reuse a look I've made before?* — the inverse of "does it match the reference." Atelier costs more tokens and iteration than templated; say so at proposal so the user opts in knowingly.

**Headless video jobs use the video kit** - a third authoring mode, and the one a job brief
means when it says "compose with the video kit". It reuses ENGINE, never a finished look: shot
sequencing, cuts pinned to spoken words, transitions, clip playback, captions, the soundtrack and
the export are a tested library (`remotion-composer/kit/`; its reference is preloaded at the end
of this guide), and the director writes the video as an edit file. This holds for whatever
pipeline the request routes to. A pipeline's stage skills keep their say on WHAT the piece needs
(structure, pacing, grade, what each beat shows); where one tells you to choose a render runtime,
to assemble stock scene-types, to hand-author a composition or to render with `video_compose`,
the kit replaces that step: record `render_runtime: "remotion"` as given (the kit renders with
Remotion), build the piece as the edit, and render it with `kit render`. The lock rules of a
headless job are the same in every pipeline (checkpoint-protocol -> "Boxed-Job Efficiency";
the batch and burst patterns in `skills/pipelines/avatar-spokesperson/asset-director.md`).
What is creative stays yours: the look is copied into the job as editable code and MUST be restyled for the video
(colours, type, the feel of its cards - `kit check` refuses a final render while it is
untouched), and any shot or overlay may be a scene written for this video. Close with the same
distinctness review: could this be any other product's video? If yes, restyle further or write
the scene that makes it this one.

### Escalate Blockers Explicitly

When a blocker occurs, the agent must surface it immediately using this structure:

1. What was attempted
2. What failed
3. Whether the issue is auth, provider access, tool bug, or prompt/design quality
4. What options exist next
5. Which option the agent recommends, with reasoning

Do not continue with a substitute path until the user approves.

### Recommendation Style

When asking the user to choose, do not just list options. The agent should:

- provide the shortlist,
- explain the tradeoffs briefly,
- recommend one option,
- wait for approval before proceeding.

### No Unilateral Substitutions

If the approved path is blocked, the agent may investigate and prepare alternatives, but may not execute those alternatives without user approval.

This applies especially to:

- provider swaps,
- model swaps,
- fallback tools,
- prompt-only substitutes for reference-driven generation,
- still-image animatics in place of true motion.

## Orchestrator

The agent itself orchestrates the production state machine:

`research -> proposal -> script -> scene_plan -> assets -> edit -> compose`

The agent:

1. Reads the pipeline manifest (`pipeline_defs/*.yaml`) to know the process
2. Calls `checkpoint.get_next_stage()` to find where to resume
3. Reads the stage's director skill (`skills/pipelines/<pipeline>/<stage>-director.md`) to know HOW
4. Uses tools (`tools/`) for concrete capabilities
5. Self-reviews using the reviewer meta skill (`skills/meta/reviewer.md`)
6. Checkpoints via the checkpoint protocol (`skills/meta/checkpoint-protocol.md`)
7. Presents to human for approval when `human_approval_default: true`

Infrastructure files:

- `lib/checkpoint.py` — read/write checkpoints, stage validation
- `tools/cost_tracker.py` — budget governance
- `lib/pipeline_loader.py` — manifest loading and helpers

## Project Directory Convention

Every production run creates a project workspace under `projects/`. This directory is gitignored — all generated assets are regenerable.

```
projects/<project-name>/
├── artifacts/          # JSON artifacts from each stage (research_brief, script, scene_plan, etc.)
├── assets/
│   ├── images/         # Generated images (PNG)
│   ├── video/          # Generated video clips (MP4)
│   ├── audio/          # Narration segments + final mix (MP3/WAV)
│   ├── music/          # Background music track (MP3)
│   └── subtitles.srt   # Generated subtitles
└── renders/
    └── final.mp4       # Final rendered video (the deliverable)
```

**Naming convention**: Use kebab-case derived from the video title (e.g., `hidden-math-of-nature`, `how-music-rewires-brain`).

At pipeline initialization, before any stage runs:

1. **Initialize the workspace**: `python -c "from lib.checkpoint import init_project; init_project('<project-id>', title='<Title>', pipeline_type='<pipeline>')"` — creates the layout above and writes `project.json` (the marker the Backlot board reads).
2. **Open the board**: run `python -m backlot open <project-id>`. This starts the Backlot server if needed and opens the user's browser at the project's live board. If the command fails, continue the production — the board is an observer, never a blocker. This is the agent's ONLY board duty; the board derives everything else from disk.

All tools and agents must write outputs to these paths — **always pass an explicit `output_path` under `projects/<project-id>/`**. Assets written to the repo root, cwd, or temp dirs are invisible to the user's board and violate the workspace contract.

**This applies to atelier and HyperFrames-skill runs too**: hand-authored compositions still write the canonical artifacts they have (script or beats-plan, scene_plan-equivalent, asset manifest) plus checkpoints into `projects/<project-id>/`. The board is runtime-agnostic; only runs that skip the artifacts get a degraded board.

## Music Library

Users can place royalty-free music tracks in `music_library/` (gitignored). The asset director will check this folder before falling back to API-based music generation.

```
music_library/
├── ambient_track.mp3
├── cinematic_epic.mp3
└── ...
```

If the folder has tracks, the proposal and asset stages should present them as options alongside generated music. See the proposal-director and asset-director skills for details.

## Available Pipelines

| Pipeline | Best For | Stability |
|----------|----------|-----------|
| `animated-explainer` | Topic to fully generated explainer | production |
| `talking-head` | Footage-led speaker videos | beta |
| `screen-demo` | Screen recordings and walkthroughs | production |
| `clip-factory` | Many clips from one long source | beta |
| `podcast-repurpose` | Podcast highlights and derivatives | beta |
| `cinematic` | Trailer, teaser, and mood-led edits | production |
| `animation` | Motion-graphics and animation-first videos | production |
| `character-animation` | Local rigged cartoon characters and reusable character acting | beta |
| `hybrid` | Source footage plus support visuals | production |
| `avatar-spokesperson` | Presenter-led avatar or lip-sync videos | production |
| `localization-dub` | Subtitle, dub, and translated variants | beta |
| `framework-smoke` | Test: minimal 2-stage smoke test | test |

> **Beta pipelines** have not been fully audited. They work, but expect rough edges. Mention this when the user selects one.

## Headless Video API (`api/` + `engine/`)

The repo ships a thin async FastAPI job layer (`api/`, `engine/`) so a SaaS can request videos over
HTTP: submit a prompt + references, poll `GET /v1/jobs/{id}`, download files. Videos themselves are
produced by the standard agent-driven pipelines above - the job layer contains no production logic
(the unattended agent runner is still being built; until then `POST /v1/videos` answers 501). An
optional internal tool API (`OPENMONTAGE_ENABLE_TOOL_API=1`) runs single registry tools as jobs.
Reference: [`docs/API.md`](docs/API.md).

- Route work here only when the user asks about the API, the Docker image, or the `engine/` / `api/`
  code. Interactive video requests in chat still go through the pipelines (Rule Zero).
- Run locally: `uvicorn api.main:app --port 8080 --workers 1` (one worker per process, by design).
- Tests: `pytest tests/api tests/engine` (no network).

## Latency Rules (Binding)

Wall-clock time is a product feature: headless jobs are billed and judged by it.

1. **Independent paid generations run concurrently in ONE Bash call.** The harness
   executes tool calls sequentially even when you issue several in one message, so the
   parallelism must live inside the call: ThreadPoolExecutor (or `&` + `wait`) over the
   submissions, one result JSON per item. The binding pattern is in
   `skills/pipelines/avatar-spokesperson/asset-director.md` -> "Parallel Generation
   (Binding)"; it applies to every pipeline's independent batches (presenter takes,
   b-roll motions, scene stills).
2. **Batch independent commands.** Several quick, independent checks belong in one Bash
   call with labeled output - every extra turn costs a thinking gap.
3. **Fold bookkeeping into the producing call.** cost_log appends and `in_progress`
   partial progress ride in the same Bash call that generated the item
   (checkpoint-protocol -> "Boxed-Job Efficiency"); never spend a turn on them.
4. **Read preloaded context instead of re-deriving it.** The preflight writes
   `work/provider_menu.json` (below), and the end of this guide preloads the checkpoint
   digest and the video kit's reference - do not spend turns re-reading their source
   files. The composition workspace is pre-staged at `composition/` (the kit's entry
   file, an empty scene registry, an edit skeleton, a starter look to restyle and a
   node_modules link) - extend it, never rebuild it; only if it is missing, run
   `kit init`.
5. **Pipeline the stages.** At script lock, fire the whole provider burst as ONE
   detached process (the Parallel Generation pattern, chained: TTS first,
   concurrently; then `kit voice`; then takes/b-roll/music/image concurrently as
   their inputs land, each writing its result JSON and cost-log entries). Write it as
   a script and start it from a Bash call that returns at once:
   `nohup sh -c 'python work/burst.py; echo "BURST EXIT $?"' > work/burst.out 2>&1 &`
   (the shell prints the last line however the script ends). The Bash tool's own
   background mode is switched off in jobs, and a foreground call would hold the
   session until the last take lands. While it cooks, restyle the look and settle the
   layout with `kit sheet --draft` (it shows a waiting frame for whatever is still
   generating; never make a stand-in file yourself); collect at edit time, and if
   it is still running then, wait for it in ONE call
   (`until grep -q "BURST EXIT" work/burst.out; do sleep 3; done`). Before writing
   RUN_REPORT.md, verify the burst has finished and every expected output file
   exists; resubmit only the failed items (their per-item files are idempotent).
   Never end the session with a detached process still running.
6. **Delegate mechanical work.** See "Delegation (Binding)" just below.

## Delegation (Binding)

The box has two subagents (the Task tool). Several Task calls issued in ONE message run
concurrently while you wait inside the turn - delegated phases finish in max() time,
not sum(). Foreground only, and never poll: a background burst or a spawned subagent is
launched, other work happens, and results are collected ONCE at the stage boundary.
Repeated status-check Bash calls are wasted turns and are a protocol violation.

- **`composer`** - the custom-scene author (its own LLM). The video kit assembles the
  video from your edit, so there is no composition to draft; the composer writes ONLY the
  scenes the kit does not have, in `composition/src/scenes/`, registers them and
  typechecks them. Spawn it when - and only when - the plan lists custom scenes. Its brief
  must be self-contained: the absolute project path, each scene's name, what it shows and
  when, its props, and the look it has to belong to. CREATIVE AUTHORITY stays with you:
  review what comes back, make small fixes yourself, and own the final cut.
- **`mechanic`** - the cheap mechanical worker: QA frame extraction and mechanical checks
  (durations, dimensions, decode checks, file existence), file wrangling and probes.
  Staging, word timing and the mix are `kit` commands now - never hand them to a subagent.

**The one-message spawn (MUST, at script lock in headless jobs):** the moment the plan, the
script, the shopping list and `composition/edit.json` are locked (`kit check --draft`
passes; a custom scene the plan lists but nobody has written yet is reported as still to
write and does not block the lock), issue ONE message that starts the whole provider burst
as a single detached Bash call (Latency rule 5). It returns at once, and the rest happens
while the burst cooks, in this order: restyle the look; then `Task composer` when the plan
lists custom scenes (after the look, so the scenes can wear it); then `kit sheet --draft`,
look at it and fix what it shows. When the burst is back, collect it once and continue.
Writing sequencing, caption, transition or clip-playback code yourself - or having a
subagent write it - is a protocol violation, not initiative: that code is the kit's.

**Never delegate:** creative decisions (script, casting, shot choices, the edit, titles,
music direction); QC verdicts (reading `skills/meta/reviewer.md` and judging against it
stays yours); paid generation calls; decision-log judgment entries; `kit` commands, the
final render and settlement; anything whose brief you cannot make fully self-contained.

**Rules:** foreground only - never run a subagent in the background; at most 3 at
once; every brief names its exact input and output paths ABSOLUTE under the project
directory (subagents inherit the session's repo-root cwd, which is read-only in the
box - a relative path silently lands outside the project) plus the check that proves
completion; verify each subagent's result yourself before building on it.

## Mandatory Preflight

In a headless job the box writes the provider rollup to **`work/provider_menu.json`**
in the project directory at job start. **Read that file first** (fold the `cat` into
your first Bash call); run `provider_menu_summary()` yourself only if the file is
missing - and if a later tool call contradicts it, trust the live registry and say so.

Do this before any creative work. **Use `provider_menu_summary()` first — it's the human-ready rollup.** The raw `support_envelope()` dump is a firehose (megabytes of JSON on a well-configured machine); pasting it into chat will bury the user.

```bash
python -c "
from tools.tool_registry import registry
import json
registry.discover()
print(json.dumps(registry.provider_menu_summary(), indent=2))
"
```

The summary returns four fields the agent should translate into plain language:

- `composition_runtimes` — booleans for `ffmpeg`, `remotion`, `hyperframes`. This is the source of truth for the "Present Both Composition Runtimes (HARD RULE)" check.
- `capabilities[]` — one entry per capability family with `configured / total` counts and provider lists. Ready-made for the "N of M configured" menu.
- `setup_offers[]` — unavailable tools whose install is a 1-minute env-var fix. Lead with these when offering upgrades.
- `runtime_warnings[]` — specific signals like "hyperframes: npm package not resolvable". Surface these to the user verbatim — they're the kind of silent-failure bugs that break the governance contract.

Then, for deeper inspection (only when the summary isn't enough):

```bash
# Full menu — grouped available/unavailable per capability.
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.provider_menu(), indent=2))"

# Raw envelope — every tool's full contract. Slow/firehose; use for debugging only.
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.support_envelope(), indent=2))"
```

Then:

1. Read the selected manifest in `pipeline_defs/`.
2. Check every `required_tools` entry against the registry.
3. Check `fallback_tools` for unavailable tools.
4. Report one of: `passed`, `degraded`, or `blocked`.
5. Do not start production until the user understands the real capability envelope.

### Provider Menu (Mandatory at Preflight)

Already fetched via `provider_menu_summary()` above. Read that output and **present it to the user as a capability menu**, not as a flat tool list. Use `provider_menu()` directly only when you need the per-tool detail the summary collapses.

**How to present:**

```
YOUR CAPABILITIES

  Video Generation:  0/13 configured
  Image Generation:  1/7 configured
  Text-to-Speech:    1/3 configured
  Music Generation:  1/1 configured
  Composition:       3/3 configured (FFmpeg, video_stitch, video_trimmer)

  You can produce videos now with images + TTS + FFmpeg.
  Quick upgrades available — see below.
```

For EACH capability with unavailable providers, read the `install_instructions` field from the menu output and present setup options grouped by effort:

```
QUICK SETUP OPTIONS (1-minute each — set an env var in .env)

  Video Generation (0/13 -> unlock the biggest upgrade):
    Each unavailable provider lists its own install_instructions.
    Read them from the provider_menu output and present grouped by env var.
    Example: if 3 tools need FAL_KEY, group them: "FAL_KEY unlocks 3 providers"

  Image Generation (1/7 -> more style options):
    Same pattern — read install_instructions from each unavailable tool.

  Text-to-Speech (1/3):
    Same pattern.

LOCAL OPTIONS (free, needs hardware):
  Tools with runtime=LOCAL or runtime=LOCAL_GPU — read from the menu.

Already Available:
  List what's working. The user should feel good about what they have.
```

**Rules:**
- Do NOT hardcode provider names, API key names, or setup URLs in your prompts.
  Read them from the registry's `install_instructions` field on each tool.
- Always show the ratio: "X of Y configured" — this makes breadth visible.
- Group by capability, not by individual tool.
- Show what they CAN do now, then what they COULD unlock.
- If the user declines setup, proceed with the best available path — no nagging.
- If a tool shares an env var with others, group them (read from `dependencies` field).

### Setup Offer Protocol

When tools are `UNAVAILABLE` but can be fixed with simple configuration, **offer the user setup help instead of silently working around the limitation.** Many tools are one env var away from working.

| Fix Complexity | Action |
|----------------|--------|
| **1-minute fix** (env var) | Offer to help configure now — read `install_instructions` from the tool |
| **5-minute fix** (install) | Explain what to install and why — read `install_instructions` from the tool |
| **Complex fix** (GPU, model download) | Note the limitation, explain what it would unlock, move on |

**Rules:**
- Always tell the user what they're missing AND what they'd gain
- Show the cost difference (free local vs. paid API)
- If the user declines setup, proceed with the best available path — no nagging
- Group related fixes (tools sharing the same env var dependency)

### Composition Runtimes (Inside video_compose)

`video_compose` has **three** render engines / runtimes. They are parallel, not ranked — the choice is made at proposal and locked in `edit_decisions.render_runtime`. Check which are available:

```bash
python -c "
from tools.tool_registry import registry
registry.discover()
info = registry._tools['video_compose'].get_info()
print('Render engines:', info.get('render_engines'))
print('Remotion note:', info.get('remotion_note'))
print('HyperFrames note:', info.get('hyperframes_note'))
"
```

| Engine | Used For | Requires |
|--------|----------|----------|
| **FFmpeg** | Video-only cuts, concat, trim, subtitle burn | `ffmpeg` binary (always available) |
| **Remotion** | React-based composition: still images → animated video, text cards, stat cards, charts, callouts, comparisons, transitions with spring physics, word-level caption burn, TalkingHead avatar | Node.js (`npx`) + `remotion-composer/` + `node_modules` |
| **HyperFrames** | HTML/CSS/GSAP composition: kinetic typography, product promos, launch reels, website-to-video, registry-block-driven scenes, SVG character rigs | Node.js ≥ 22 + FFmpeg + `npx` (consumed via `npx hyperframes`) |

`render_runtime` is **locked at proposal** (`proposal_packet.production_plan.render_runtime`) and **carried through edit_decisions unchanged**. `video_compose` routes based on this field; silent runtime swaps are forbidden. If the chosen runtime becomes unavailable at compose time, surface a structured blocker per "Escalate Blockers Explicitly" above. See `skills/core/hyperframes.md` for the Remotion-vs-HyperFrames decision matrix.

### Critical Rule: Motion-Required Requests

For any request where the deliverable inherently depends on motion rather than static coverage, treat motion as a hard requirement. Examples:

- sci-fi trailers,
- cinematic teasers built from generated clips,
- hype edits,
- avatar or agent videos,
- any brief whose promise depends on moving shots rather than still frames.

For these requests:

- The `render_runtime` chosen at proposal (Remotion, HyperFrames, or FFmpeg) must be confirmed available up front if the planned visual treatment depends on it.
- Still-image fallback is forbidden. Do not quietly convert the job into a Ken Burns teaser, animatic, or slide-based video.
- FFmpeg-only fallback is forbidden when it changes the approved deliverable from motion-led video to still-led video.
- **Silent runtime swap is forbidden.** If `render_runtime="hyperframes"` was locked and HyperFrames is unavailable, do NOT route to Remotion instead. Surface the blocker, propose options, get user approval, log a `render_runtime_selection` decision — then proceed.
- Bubble critical issues immediately. If the chosen runtime is unavailable, fails to render, or provider clip generation fails in a way that blocks the approved treatment, stop and tell the user before proceeding.
- Do not spend more tokens or time on downgraded output unless the user explicitly approves the downgrade as an animatic or proof-of-concept.

**When Remotion is available**, the agent should design production plans around it:
- Explainer videos with `flat-motion-graphics` playbook -> Remotion animated scenes, not Ken Burns
- Data-driven videos -> Remotion stat cards and charts, not static image screenshots
- Any pipeline using still images -> Remotion spring animations, not FFmpeg pan-and-zoom
- **Screen demos of a CLI/terminal/install flow -> `TerminalScene` (synthetic screen recording), not OS-level capture.** See `.agents/skills/synthetic-screen-recording/SKILL.md`. Faster, deterministic, privacy-safe. Use real capture (`screen_recorder`, `cap_recorder`, `playwright-recording`) only when the demo is a real app UI or requires unpredictable live behavior.

### Remotion scene types available in `remotion-composer/`

See `remotion-composer/SCENE_TYPES.md` for the authoritative list and their cut schemas. Current scene types usable via `cut.type`:
`text_card`, `stat_card`, `callout`, `comparison`, `hero_title`, `terminal_scene`, `anime_scene`, `bar_chart`, `line_chart`, `pie_chart`, `kpi_grid`, `progress_bar`. Overlay types include `section_title`, `stat_reveal`, `hero_title`, `provider_chip`.

These stock scene-types are the **templated** path — fast and reliable, but they are why videos look alike. For **hero work, prefer atelier mode** (hand-authored composition) over this catalog; read those types as a *mechanics codex*, not a menu to assemble. See "Composition Authoring Mode" above and `skills/meta/bespoke-composition.md`.

**When Remotion is NOT available** and `render_runtime="remotion"` was NOT locked, `video_compose` may use FFmpeg Ken Burns motion on still images. This still works but produces less engaging visuals. Mention this tradeoff in the proposal. When `render_runtime="remotion"` IS locked and Remotion is unavailable, that's a blocker — escalate, don't silently swap.

When `render_runtime="hyperframes"` is locked and HyperFrames is unavailable (Node < 22, missing `ffmpeg`/`npx`, or `hyperframes doctor` reports issues), that's also a blocker. Do not substitute Remotion or FFmpeg without user approval + a logged `render_runtime_selection` decision.

Routing is automatic — `video_compose` reads `edit_decisions.render_runtime` and dispatches to the matching engine (`_render_via_hyperframes`, `_remotion_render`, or `_render_via_ffmpeg`). But the **agent must know both Remotion and HyperFrames exist at proposal time** so it can design the visual approach intentionally. Don't default to Remotion for motion-graphics-heavy concepts that HTML/GSAP would express more naturally, and don't default to HyperFrames for briefs that reuse the existing React scene stack.

## Capability Discovery

OpenMontage uses two layers for capability choice:

- selector tools: capability-level routing such as `tts_selector` and `video_selector`
- provider tools: concrete tools discovered via the registry that call a specific backend

Always inspect the registry first:

```bash
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.capability_catalog(), indent=2))"
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.provider_catalog(), indent=2))"
```

For finalist tools inspect:

- `capability`
- `provider`
- `usage_location`
- `supports`
- `fallback_tools`
- `related_skills`

Do not rely on memory or old docs when the registry can answer it.

## Tool Families

**Do not maintain hardcoded tool lists.** Always query the registry at runtime:

```bash
# See all tools grouped by capability (TTS, video_generation, image_generation, etc.)
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.capability_catalog(), indent=2))"

# See all tools grouped by provider (elevenlabs, openai, ffmpeg, etc.)
python -c "from tools.tool_registry import registry; import json; registry.discover(); print(json.dumps(registry.provider_catalog(), indent=2))"
```

Key capability families to look for in the output:

- **tts** — Text-to-speech providers. Route via `tts_selector`.
- **video_generation** — Video generation providers (cloud, local GPU, stock). Route via `video_selector`.
- **image_generation** — Image generation providers (cloud, local GPU, stock). Route via `image_selector`.
- **music_generation** — Music and sound effect generation.
- **video_post** — Composition, stitching, trimming (FFmpeg-based, always local).
- **audio_processing** — Mixing, enhancement (FFmpeg-based, always local).
- **analysis** — Transcription, scene detection, frame sampling.
- **avatar** — Talking head and lip sync generation.
- **character_animation** — Local character specs, SVG rigs, pose libraries, action timelines, previews, and QA.
- **3d_world_generation** — Local semantic terrain, procedural biome scattering, explicit landmarks, diagnostic passes, and deterministic HyperFrames/Three.js camera fly-throughs. Route through `threejs_world` and read `skills/creative/3d-world-generation.md` plus `.agents/skills/threejs-world-generation/SKILL.md`.
- **3d_asset_acquisition** — Rights-safe GLTF/GLB catalogs for production-tier Three.js worlds. Route through `threejs_asset_catalog`; never substitute blockout primitives for a requested detailed or reference-grade environment.
- **3d_asset_generation** — Unique textured/PBR meshes from text or concept images. Route text-described hero assets through `atlas_3d`, image-conditioned assets and regional object extraction through `fal_3d`, and read `.agents/skills/3d-asset-generation/SKILL.md`. Announce provider/model/unit cost before every first paid call and sample before batching.
- **3d_world_rendering** — Production assembly, terrain, lighting, materials, camera, and image-sequence rendering in Blender. Route through `blender_world`; use Three.js for interactive/blockout review, not as a substitute for Blender when reference-grade scene density is requested.
- **enhancement** — Upscale, background removal, face enhance, color grading.

Each tool in the registry declares `best_for`, `install_instructions`, `runtime` (LOCAL, API, LOCAL_GPU, HYBRID), and `status`. Read these fields — do not assume tool strengths from memory.

### Tool Class Naming Convention

All tool classes use **PascalCase without a "Tool" suffix**. When importing tools in Python:

| Module | Class Name | NOT |
|--------|-----------|-----|
| `tools.audio.music_gen` | `MusicGen` | ~~MusicGenTool~~ |
| `tools.video.video_compose` | `VideoCompose` | ~~VideoComposeTool~~ |
| `tools.audio.audio_mixer` | `AudioMixer` | ~~AudioMixerTool~~ |
| `tools.tts.elevenlabs_tts` | `ElevenLabsTTS` | ~~ElevenLabsTTSTool~~ |
| `tools.analysis.transcriber` | `Transcriber` | ~~TranscriberTool~~ |
| `tools.subtitle.subtitle_gen` | `SubtitleGen` | ~~SubtitleGenTool~~ |

When in doubt, check: `grep "^class " tools/<path>.py`

All tools call via `.execute(params_dict)` (returns `ToolResult` with `.success`, `.data`, `.error`), NOT `.run()`.

### Selector Pattern

Three selector tools abstract multi-provider capabilities. **Selectors auto-discover providers from the registry.** Adding a new provider tool automatically makes it available through the selector — no selector code changes needed.

| Selector | Routes to | How it discovers |
|----------|-----------|-----------------|
| `tts_selector` | All tools with `capability="tts"` (ElevenLabs, Google TTS, OpenAI, Piper, Azure, fish.audio) | `registry.get_by_capability("tts")` |
| `image_selector` | All tools with `capability="image_generation"` (FLUX, Google Imagen, GPT Image, Recraft, etc.) | `registry.get_by_capability("image_generation")` |
| `video_selector` | All tools with `capability="video_generation"` | `registry.get_by_capability("video_generation")` |

Selectors route based on: user preference > availability > discovery order. They adapt input schemas between providers transparently.

## User-Facing Planning Protocol

Before committing to execution, present:

1. `4-5` concept directions when the brief is still open.
2. Recommended pipeline.
3. Recommended tool path.
4. Alternative tool paths that are actually available.
5. Cost estimate and quality tradeoffs.
6. **Music plan** — mandatory for every pipeline that has audio. See below.
7. Production plan by stage.
8. Approval gate before asset generation.

If a user prefers a specific vendor and that tool is available, surface it directly. Do not hide provider choice.

### Music Plan (Mandatory)

Music is a critical part of any video. **Surface the music situation to the user at proposal/idea time** — do not silently defer it to the asset stage where a failure becomes expensive.

Check music availability in this order and present the options:

1. **User music library:** Check `registry.get_by_capability("music_library")` and inspect `music_library/`. If tracks exist, list durations and let the user pick one.
2. **Royalty-free search:** Check `registry.get_by_capability("music_search")` for configured search/download tools. Report licensing constraints and whether a key is required.
3. **Music generation APIs:** Check `registry.get_by_capability("music_generation")`. Report status, quota, cost, and quality tradeoffs honestly.
4. **Bring your own:** Note that the user can provide a track (for example from YouTube Audio Library or Jamendo) through the `music_library/` drop path.

**Always present the user with explicit choices:**
- Use a track from their library (which one?)
- Provide a different track (drop it in `music_library/`)
- Generate one via API (if available — name the provider and cost)
- Proceed without music

**If no music source is available:** Tell the user explicitly. Do NOT let this surface as a surprise at the asset stage.

Record the music decision in the proposal/brief artifact so the asset director knows what to do.

## Pipeline Asset Expectations

Each pipeline manifest's `tools_available` field declares what tools a stage can use. Use selectors for multi-provider capabilities — the selector handles routing to whatever is available. Read the pipeline manifest for the authoritative list per stage.

## Stage Agents

Each stage produces one canonical artifact that becomes the contract for the next stage. The stage director skill teaches the agent HOW to produce it.

| Stage | Director Skill | Canonical output | Core quality bar |
|------|---------------|------------------|------------------|
| `idea` | `*-director.md` | `brief` | Clear hook, target platform, duration, tone, and user intent |
| `script` | `*-director.md` | `script` | Structured sections, valid timing, coherent narration |
| `scene_plan` | `*-director.md` | `scene_plan` | Ordered scenes, timings, asset requirements |
| `assets` | `*-director.md` | `asset_manifest` | Provenance, paths, model/tool metadata, scene linkage |
| `edit` | `*-director.md` | `edit_decisions` | Concrete cuts, overlays, subtitle/music decisions |
| `compose` | `*-director.md` | `render_report` | Output paths, encoding profile, verification notes |

Stage contract rules:

- A completed or awaiting-human checkpoint must include the stage's canonical artifact.
- Canonical artifacts must validate against the JSON schema in `schemas/artifacts/`.
- Non-canonical outputs such as media files belong in stage-specific directories.
- Tools should record seeds/model versions for reproducibility.

## Reviewer Protocol

The reviewer is a meta skill (`skills/meta/reviewer.md`) — advisory, never directly blocks progression.

- Self-review after every stage execution, before checkpointing.
- Load `review_focus` items from the pipeline manifest for the current stage.
- Maximum two review rounds. After that, pass with warnings and move on.
- Findings categorized: critical (must fix), suggestion (should fix), nitpick (nice-to-have).
- Critical findings -> fix and re-review. Suggestions -> note and proceed.
- Check playbook `quality_rules` as constraints, not suggestions.

## Human Checkpoint Protocol

The checkpoint protocol meta skill (`skills/meta/checkpoint-protocol.md`) teaches the agent when to pause:

- Read `human_approval_default` from the pipeline manifest per stage. **The manifest value is binding** — never re-judge it. `lib/checkpoint.py` enforces this: a gated stage cannot be written `completed` without `human_approved=True`.
- Typical gated stages: `idea`/`proposal`, `script`, `scene_plan`, **`assets`** (review the generated assets scene-by-scene — the Backlot board's filmstrip — before compose locks them in), and `publish` where the pipeline has one. Most pipelines auto-proceed on `edit` and `compose`, but not all (documentary-montage gates `edit`) — the manifest you loaded is the only authority.
- When approval is required: write the checkpoint as `awaiting_human`, present artifact summary, review findings, and cost snapshot — then **END YOUR TURN**. Doing further pipeline work in the same response is a gate violation.
- **Approval is per-gate.** An early "go ahead" never covers later gates; explicit full-run pre-authorization must be recorded as a `decision_log` entry (`category: "approval_policy"`) to count.
- Wait for human to approve, request revision, or abort.

## Communication Protocol

Agents coordinate through canonical JSON artifacts, checkpoints, pipeline manifests, and the tool registry.

Primary files:

- Artifact schemas: `schemas/artifacts/`
- Checkpoint schema: `schemas/checkpoints/checkpoint.schema.json`
- Pipeline manifest schema: `schemas/pipelines/pipeline_manifest.schema.json`
- Pipeline manifests: `pipeline_defs/`
- Style playbooks: `styles/*.yaml` (validated by `schemas/styles/playbook.schema.json`)
- Tool contract: `tools/base_tool.py`
- Tool registry: `tools/tool_registry.py`
- Stage director skills: `skills/pipelines/<pipeline>/<stage>-director.md`
- Meta skills: `skills/meta/*.md`

Checkpoint rules:

- Checkpoints live at `projects/<project_id>/checkpoint_<stage>.json` (the project workspace — this is what the Backlot board watches).
- `status` may be `completed`, `failed`, `awaiting_human`, or `in_progress`.
- Write an `in_progress` checkpoint on entering each stage; during `assets`/`compose`, refresh `metadata.partial_progress` after each completed scene/asset unit — this powers live progress on the board.
- `completed` and `awaiting_human` checkpoints must include the canonical artifact.
- A gated stage (`human_approval_default: true`) can only be written `completed` with `human_approved=True` — the writer raises a GATE VIOLATION otherwise.
- Superseded checkpoints are archived automatically to `projects/<project_id>/history/` — stage re-runs never destroy run history.
- Invalid checkpoints or invalid canonical artifacts are contract violations and should fail fast.

Pipeline manifest rules:

- Pipelines are declarative YAML manifests in `pipeline_defs/`.
- Stages declare: `skill` (director skill path), `produces`, `tools_available`, `review_focus`, `success_criteria`, `human_approval_default`.
- Adding a new pipeline requires a manifest + stage director skills.

Tool rules:

- Every production tool must inherit from `BaseTool`.
- Tool discovery flows through the registry, not ad hoc imports.
- Support-envelope reporting is the source of truth for capability, status, and resource requirements.

## Style Playbooks

| Playbook | Best For |
|----------|----------|
| `clean-professional` | Corporate, educational, SaaS |
| `premium-minimalist` | Investor updates, expert explainers, product narratives |
| `flat-motion-graphics` | Social media, TikTok, startups |
| `minimalist-diagram` | Technical deep-dives, architecture |
| `ink-sketch` (Ink Theater) | Hand-drawn ink-on-white doodle animation; a character that draws itself, walks, dances; contraption explainers |

For custom, atelier, brand, launch, or hero work, read `skills/meta/taste-direction.md` before choosing a playbook. Carry its `taste_profile` into the proposal so later stages can preserve the design read, visual variance, motion intensity, information density, reference strategy, and anti-patterns.

### Hand-drawn "doodle" animation → Ink Theater / Ink Puppet

For any brief that wants a **hand-drawn ink doodle** look — "a sketch that comes to life", "a pencil / stick figure that walks or dances", "a little character that acts out the idea", whiteboard-doodle explainers — use the **Ink Theater** engine + **Ink Puppet** mocap system (`skills/creative/ink-theater.md`, `ink-theater/README.md`). It is a **style + reusable engine, not a new pipeline**: illustration / contraption pieces run on the `animation` pipeline; a mocap character (draws itself → walks / dances / waves via `InkPuppet.choreograph([...])`) runs on `character-animation`. Cross-tool entry points: **`/ink-art`** (create a vector doodle from scratch) and **`/animated-drawing`** (animate a *supplied* drawing with mocap — raster; `skills/creative/animated-drawing.md`). Never hand-tune character motion — the agent only chooses named mocap clips.

## Layer Map

OpenMontage has three instruction layers:

1. `tools/`
   What exists, what is available, cost, runtime, fallback, related skills.
2. `skills/`
   How OpenMontage wants those tools used in pipelines.
3. `.agents/skills/`
   Raw vendor or technology knowledge.

Reading order:

1. registry / tool contract — discover what's available
2. relevant pipeline or creative skill (Layer 2) — know HOW to use it in this context
3. underlying vendor skill (Layer 3) — **mandatory before calling any generation tool**

**Prefer skills over source code for tool usage.** Skills exist precisely so you don't need implementation details in the common case. Layer 2 tells you *what* and *when*. Layer 3 tells you *how*. For authoring prompts, choosing parameters, or understanding usage patterns, you should be reading skills — not `.py` files.

**Exception: debugging, audits, and verifying the governance contract.** When a skill and a tool disagree, or when something behaves differently than the skill claims, reading the tool source is fair game — that's often the only way to catch a silent-availability bug or a stale doc string. An audit that refuses to look at the implementation will miss exactly the bugs that matter most. If you do read source to debug, consider whether the finding belongs in a skill update afterward so the next agent doesn't need to repeat the dive.

**Layer 3 is not optional.** Every generation tool (video, image, TTS, music) has an `agent_skills` field listing its Layer 3 skills. These skills contain provider-specific prompt engineering, parameter tuning, and quality techniques. Read them before writing prompts. The difference between a generic prompt and a skill-informed prompt is the difference between "usable" and "cinematic."

Example: Before calling `kling_video`, read its `agent_skills` → `ai-video-gen` → get Kling-specific prompt structure, camera direction syntax, and quality keywords that the model responds to best.

### Layer 3 skills, by category

The `.agents/skills/` directory is large. When you're not coming in through a tool's `agent_skills` pointer, use this table to find the right file by *what you're trying to do*:

| Category | Skills |
|---|---|
| **Composition runtime** | `remotion`, `remotion-best-practices`, `synthetic-screen-recording` (fake terminal/UI demos via Remotion TerminalScene), `threejs-world-generation` (semantic terrain and free-viewpoint HyperFrames worlds) |
| **Animation knowledge (generic)** | `gsap-core`, `gsap-timeline`, `gsap-plugins` (SplitText / MorphSVG / DrawSVG / MotionPath / Flip / CustomEase), `gsap-utils`, `gsap-react`, `gsap-performance`, `gsap-scrolltrigger`, `gsap-frameworks`, `framer-motion` (Disney 12 principles), `lottie-bodymovin` (Lottie export) |
| **Character animation** | `character-rigging`, `svg-character-animation`, `pose-library-design`, `canvas-procedural-animation`, `character-animation-qa` |
| **Image generation** | `bfl-api`, `flux-best-practices` |
| **Video generation** | `seedance-2-0` (preferred premium default — cinematic, trailer, multi-shot, synced audio, lip-sync), `gemini-omni` (conversational video editing, reference tags, timecoded beats), `ai-video-gen`, `ltx2` |
| **Audio** | `elevenlabs`, `music`, `sound-effects`, `acestep`, `text-to-speech`, `azure-text-to-speech` (optional cloud TTS — tool `azure_tts`, same Speech key as `azure_stt`), `setup-api-key` |
| **Speech-to-text** | `speech-to-text` (whisper `transcriber` — default, offline), `azure-speech-to-text` (optional cloud STT — tool `azure_stt`, preferred when `AZURE_SPEECH_KEY` is set) |
| **Avatar / lip-sync** | `avatar-video`, `heygen`, `create-video`, `faceswap`, `video-translate`, `agents` |
| **Capture** | `playwright-recording` (browser flows), `ffmpeg` (post) |
| **Visualization** | `beautiful-mermaid`, `d3-viz`, `manim-composer`, `manimce-best-practices`, `manimgl-best-practices` |
| **Media editing** | `video-edit`, `video-download`, `video-understand`, `video-toolkit`, `visual-style` |

**When in doubt, read the category's meta routing file first:**
- Picking an animation runtime? → `skills/meta/animation-runtime-selector.md` routes between Remotion primitives, GSAP plugins, framer-motion, Lottie, Manim, D3.
- Picking a screen-recording mode (real capture vs synthetic terminal)? → `pipeline_defs/screen-demo.yaml` + `skills/pipelines/screen-demo/idea-director.md`.

## Quick Lookup

| Question | Where to look |
|----------|---------------|
| What tools exist? | `tools/tool_registry.py` and `registry.support_envelope()` |
| What providers are available for a capability? | `registry.capability_catalog()` |
| What tools exist for a vendor? | `registry.provider_catalog()` |
| How does a tool actually work? | the tool's `usage_location` from the registry |
| How should this pipeline stage behave? | `skills/pipelines/<pipeline>/...` |
| What is the checkpoint/review policy? | `skills/meta/` |

## What Not To Do

- **Do not bypass the pipeline.** Never write ad-hoc scripts to call tools directly. All production goes through pipeline stages with director skills. See Rule Zero.
- **Do not call generation tools without reading their Layer 3 skill.** Check the tool's `agent_skills` field, read the referenced skill, then craft your prompts using that guidance.
- **Do not skip stage director skills.** Before executing any pipeline stage, read its director skill. The skill contains the quality bar, the workflow, and the review criteria.
- Do not use deleted legacy names such as `tts_cloud`, `tts_engine`, or `video_gen`.
- Do not hardcode provider names, API key names, or setup URLs. Read them from the registry's `install_instructions` and `dependencies` fields.
- Do not tell a restricted shared-installation user to add credentials manually, create a `.env`, or export a key. Surface the unavailable provider as an administrator setup request and continue only with centrally provisioned capabilities.
- Before offering a direct vendor credential, check the registry for an available wrapper through an already configured provider (for example, a partner model hosted by fal.ai).
- Do not begin asset generation before user approval on the production plan.
- Do not hide degraded paths. Record substitutions and blocked options explicitly.
- Do not present a single unavailable tool in isolation. Always show the full capability picture: "X of Y providers configured for this capability."
- Do not skip the Provider Menu at preflight. The user must see what they have AND what they could unlock.
- Do not change provider, model, or render path without telling the user first and getting approval when the change is material.

## Preloaded Skill Digests (Read Here, Not Mid-Run)

The checkpoint digest and the video kit's reference below are preloaded so you never
spend a turn reading their source files (a test keeps the kit's copy in sync with
`remotion-composer/kit/README.md`; the source file remains the authority if they ever
disagree). `skills/meta/reviewer.md` is deliberately NOT preloaded - read it in full at
the QC gate, as always. Writing a custom scene? The Remotion rules live in
`.agents/skills/remotion-best-practices/rules/` (videos.md, assets.md).

### Checkpoint protocol digest (authority: `skills/meta/checkpoint-protocol.md`)

- Stage flow: read the manifest's `checkpoint_required` / `human_approval_default`;
  write an `in_progress` checkpoint on stage entry; do the work; pass review; then
  `write_checkpoint(..., status="completed")` - or `awaiting_human` + END YOUR TURN
  when the stage gates (the manifest is binding; `lib/checkpoint.py` enforces it).
- `init_project()` once at pipeline start (creates the layout + `project.json`).
- Partial progress after each significant item - in boxed jobs, recorded inside the
  item's own producing call (-> "Boxed-Job Efficiency"). Schema-valid canonical
  artifacts go in `artifacts`; anything partial goes in `metadata.partial_progress`.
- On entering a stage, ALWAYS check for its `in_progress` checkpoint and resume from
  it instead of regenerating (never re-pay for work that exists).
- Checkpoints live at `projects/<id>/checkpoint_<stage>.json`; superseded ones are
  archived to `history/`, never destroyed.
- Boxed jobs require: the script checkpoint's `metadata.shopping_list` (every paid item
  with its exact artifact path) and a `composition/edit.json` that passes
  `kit check --draft`; the compose checkpoint's `metadata.kit` (the look's starter, what
  was restyled, custom scenes, sheets, renders) after a passing `kit check`; and
  `metadata.delegation` (subagent -> artifacts) whenever a subagent was used.

Read the full protocol file when a gate, resume conflict or schema question goes
beyond this digest.

### The video kit (verbatim: `remotion-composer/kit/README.md`)

# The video kit

An edit file in, a rendered video out. The director decides what is on screen and when; the
kit supplies everything that is the same in every video and was being rewritten each time:
shot sequencing, cuts pinned to spoken words, transitions, clip playback, captions, safe
zones, the soundtrack, the export.

Three layers and an escape hatch:

- **Engine** (`engine/`) - imported, never edited by a job. General building blocks only.
- **Look** (`looks/<name>/`) - copied into each job as `composition/src/look/`. Yours to
  restyle and rewrite; `kit check` refuses a final render while it is untouched.
- **The edit** (`composition/edit.json`) - the shot list as data.
- **Your own scenes** - any main item or overlay can be a component written for this video.

## A job's files

```
composition/
  edit.json          the edit
  src/index.ts       registerRoot(makeRoot({look, scenes}))   (staged, leave as is)
  src/look/          tokens.ts (colours, type, shape) and look.tsx (the components)
  src/scenes.ts      registry of scenes written for this video
  src/scenes/*.tsx   those scenes
work/kit/            voice.json, check.json, sheet.jpg, the bundle (all generated)
```

## Commands (run in the project directory)

| Command | What it does |
|---|---|
| `kit init [--look NAME]` | Stage `composition/`. Never overwrites what is there. |
| `kit look [NAME] [--force]` | List the starter looks, or copy one into the job. |
| `kit voice` | After the lines are spoken: tighten each recording, time every word of the script, split a line that is too long for one take, lay the lines end to end. |
| `kit check [--draft]` | Check the edit, the timeline and the look. `--draft` accepts files that are still generating and scenes that are not written yet. |
| `kit sheet [--draft]` | One sheet of the telling frames (about 8 s), plus layout findings measured in the browser. |
| `kit mix` | The soundtrack: voice on the spine, music under it (or the music alone when there is no voice), cut to the video's length and faded out, -14 LUFS. Run it again after anything that changes the video's length, the voice or the music. |
| `kit render [--name NAME]` | The final video in `exports/`, tagged BT.709, verified. Refuses while anything is missing, the soundtrack included. |

A draft never needs a stand-in made by hand: `check --draft` lists what is still missing
("Still generating", "Still to write") and `sheet --draft` shows a "waiting for ..." frame in
its place, also for a picture or clip that a scene takes in its props. Never put a placeholder
file at a real path: the final check would take it for the real thing.

Recordings are found at `assets/audio/<line id>.mp3` (or `.wav`), or wherever the line's
`audio` says. `kit voice` writes `assets/audio/voice_<part>.wav`: generate each take from
that file exactly as it is (no further trimming), so the take carries the audio the timings
were made from. It prints the list and writes it to `work/kit/voice.json`:
`lines[].parts[]`, each with `id`, `audio` (the prepared file), `take` (where its take belongs,
when the line has one), `start` and `dur`. A line over the take limit comes back as two parts,
so read the takes to film from this file, not from the script.

## The edit

```json
{
  "canvas": {"size": "1080x1920", "fps": 30, "safe": "tiktok"},
  "voice": {"lines": [{"id": "A", "take": true, "text": "The exact words of the line."}]},
  "music": {"file": "assets/audio/music.mp3"},
  "captions": {"zone": "lower"},
  "main": [
    {"take": "A", "until": {"word": "words"}},
    {"video": "assets/video/broll.mp4", "until": {"line": "A", "edge": "end"}, "enter": "punch"},
    {"freeze": "A", "for": 2}
  ],
  "overlays": [
    {"tag": "up to 4x", "from": {"word": "exact"}, "for": 1.6},
    {"card": "end", "props": {"title": "example.com"}, "from": {"voice": "end"}}
  ]
}
```

`voice.lines` holds every spoken line in order, with its exact words. `"take": true` means a
presenter is filmed saying it (`{"take": "A"}` then shows it); leave `take` out for a
voice-over that is only heard. A video with no voice has no lines: give every main item `for`
(or `until` in seconds), the video is as long as its items, and the soundtrack is the music.

### Moments

Anywhere a time is wanted:

| Form | Means |
|---|---|
| `3.5` or `{"t": 3.5}` | seconds from the start |
| `{"word": "rebuilds"}` | the next time the voice says it (a short phrase works too). In `main`, "next" counts from where the item starts; in overlays, from the start of the video. To pick: `"line": "C"` (the word as line C says it), `"nth": 2` (the second time), `"edge": "end"` (where the word ends). |
| `{"line": "B"}` | the start of line B (`"edge": "end"` for its end) |
| `{"voice": "end"}` | where the last line ends |
| `{"item": "m3"}` | the start of a main item (by `id`, or `m<n>` by position) |
| `{"end": true}` | the end of the video (overlays only) |

Every form takes `"offset"` in seconds. Words match whatever their case, punctuation or
hyphens. Before `kit voice` has run, timings are estimated from the script so an edit can be
checked and sheeted straight away.

### main: what fills the frame, in order

Each item starts where the one before it ended and runs `until` a moment or `for` seconds.
A take with neither runs to the end of its line; the last item runs to the end of the voice.

| Item | Shows |
|---|---|
| `{"take": "A"}` | the presenter take of line A, always on the frame that matches the voice, also after a cutaway |
| `{"video": "..."}` | a clip (`"start"` seconds in, `"speed"`, `"audio"` 0-1 to keep its sound) |
| `{"image": "..."}` | a still, with a slow push-in |
| `{"scene": "Name", "props": {...}}` | a ready-made scene or one of your own |
| `{"freeze": "A"}` | the last frame of line A's take (or of a video path) |
| `{"color": "#101010"}` | a plain colour |
| `{"layout": {...}}` | several of the above at once (below) |

Optional on any item: `"enter"` (`cut`, `fade`, `slide-left|right|up|down`, `whip`, `punch`,
`wipe`, `flash`, or `{"type", "frames"}`), `"motion"` (`push-in`, `pull-out`, `pan-left|right|up|down`,
`drift`, `hold`, `none`, or `{"from", "to", "origin"}`), `"fit"` (`cover`, `contain`, `blur`), `"id"`.
The incoming shot enters on top of the outgoing one, so a transition never moves a cut.

Layouts:

```json
{"layout": {"type": "split", "slots": [{"take": "A"}, {"video": "screen.mp4"}]}}
{"layout": {"type": "pip", "base": {"video": "screen.mp4"}, "inset": {"take": "A"}, "corner": "bottom-left"}}
{"layout": {"type": "card", "media": {"image": "wide.jpg"}, "aspect": 1.5}}
{"layout": {"type": "grid", "columns": 2, "slots": [...]}}
```

### overlays: what sits on top

`{"tag": "text"}`, `{"card": "name", "props": {...}}`, `{"text": "..."}` or
`{"scene": "Name", "props": {...}}`, each with `from` and `until` (or `for`) and an optional
`zone` (`upper`, `center`, `lower`, `full`). A tag sits in the upper zone, a `text` (a large
statement; "\n" starts a new line) in the centre. Which cards exist and what props they
take is the look's business: read `src/look/look.tsx`.

### captions

On whenever there is a voice. `{"enabled": false}` turns them off. `maxWords` / `maxChars`
tune the automatic grouping; `"phrases": ["Your AI images", "look AI", ...]` sets the breaks
(and the spelling shown) by hand. Scripts without spaces and right-to-left scripts are
handled.

## Looks

`pop` - chunky creator style: hard offset shadows, one loud accent, heavy type, springy pops.
Cards: `hook`, `cta`, `end`.

`editorial` - ink, paper chips and one signal colour; calm entrances. Cards: `end`.

A look exports `look` (see `engine/context.tsx` for the shape: `Caption`, `Tag`, `Text`,
`cards`, optional `Finish`, `Frame`, `parts`, `motion`) and, in `tokens.ts`, `tokens` and
`meta` (card zones, caption grouping, the colour pairs the legibility check reads).
`tokens.ts` must not import anything: the kit command reads it without a bundler.

Every look names the same roles, so ready-made scenes dress themselves in any look:
`color.ink`, `color.paper`, `color.accent` (plus `negative`, `positive`), `font.display`
(plus `body`, `weight`) and `shape` (`radius`, `border`, `offset`, `shadow: "hard" | "soft"`).
Keep those names when restyling; add whatever else the look needs.

Fonts. The kit ships eleven families, loaded from local files (no network): call the one a
look uses once at the top of the look and use the name it returns, for example
`const FONT = fonts.anton();` with `fonts` from `@kit/engine`. The name it returns is the
family name below, which is also what goes in `tokens.ts` (that file cannot call anything).

| | call, and the family name it returns |
|---|---|
| Heavy display | `anton` "Anton", `archivoBlack` "Archivo Black", `bebasNeue` "Bebas Neue" |
| Sans, any weight | `inter` "Inter", `montserrat` "Montserrat", `poppins` "Poppins" (500, 700, 800, 900), `spaceGrotesk` "Space Grotesk" |
| Serif | `dmSerifDisplay` "DM Serif Display", `playfair` and `playfairItalic` "Playfair Display" |
| Handwritten | `caveat` "Caveat" |

Installed on the render box and usable by name without loading: DejaVu Sans / Serif / Mono,
Noto Sans, Noto Sans Display, Noto Serif, Noto Serif Display (regular and bold), and the Noto
families for scripts beyond Latin (captions in any script fall back to them by themselves).

## Ready-made scenes

Each fills the frame as a main item; `ListReveal`, `QuoteCard` and `StatCount` also work as
overlays (`{"scene": "...", "zone": "upper"}` over footage). `backdrop` is a colour, the path
of a picture or clip, or `{"image" | "video" | "color", "blur", "dim"}`.

`BeforeAfter` - two pictures and a divider that wipes from one to the other. Props: `pair`
("refs/gran" means `refs/gran_before.jpg` and `refs/gran_after.jpg`) or `before` + `after`;
`labels`; `afterSide`; `wipe` (`start`, `seconds`, `from`, `to`); `zoom`; `frame: "card"` for
landscape pictures in a tall video. The handle and labels are drawn by the look.

`KineticText` - big words that arrive one after another; a statement that needs no footage.
Props: `text` ("\n" starts a line) or `lines`; `emphasis` (words in the accent colour);
`backdrop`; `align`; `size`; `uppercase`.

`ScreenDemo` - a screenshot or screen recording in a browser or phone frame, with a camera, a
cursor, clicks and outlined areas. Props: `image` or `video`; `frame` (`browser`, `phone`,
`plain`); `url`; `steps`, each `{"at": seconds, "zoom", "focus": [x, y], "highlight":
[x, y, w, h], "cursor": [x, y], "click", "label"}` with positions as fractions of the screen
content.

`ListReveal` - a short list whose points arrive one by one. Props: `items`; `title`; `at`
(seconds into the shot, or the word the voice says as each point lands); `numbered`.

`QuoteCard` - what a customer said. Props: `quote`, `name`, `role`, `stars`, `avatar`.

`StatCount` - a number that counts up. Props: `value`, `prefix`, `suffix`, `decimals`,
`label`, `seconds`.

## Your own scenes

```tsx
// composition/src/scenes/Orbit.tsx
import {useCurrentFrame} from 'remotion';
import {PushIn, useShot, useWordFrame} from '@kit/engine';

export const Orbit: React.FC<{image: string}> = ({image}) => {
  const frame = useCurrentFrame();          // 0 at the start of this shot
  const {frames} = useShot();               // how long the shot is on screen
  const atWord = useWordFrame('rebuilds');  // the frame the voice says it, or null
  ...
};
```

Register it in `src/scenes.ts` (`export const scenes = {Orbit};`) and name it in the edit.
The edit can name it before it is written: a draft counts it as still to write and shows a
placeholder, so the plan does not wait for the code. Media paths in props are
project-relative and are staged and checked like any other file.
`useDress()` gives the look's colours, type and surface style, `surface(d)` a card in the
look's manner, `Backdrop` what sits behind. A scene must never be empty on its first frame
(a video's first frame has to work on its own): start its entrance `HEAD_START` frames in,
as the ready-made scenes do.

## What `check` and `sheet` look for

Shape of the edit; words that are not in the script (with close matches); moments that fall
before the item starts; lines too long for one take (estimated before any voice is bought);
missing files; scenes that are misspelt or not written; frames with nothing on screen; shots
under 12 frames; text on screen too briefly to read; overlays sharing a zone or sitting in
the caption zone; colour pairs that are hard to read; a look that was never restyled; a
soundtrack that is missing, or older or of another length than the video it belongs to; and,
measured on the sheet's frames, overlays that come to rest in what the app covers or over
the captions (the moments an overlay flies in or out are not judged).

## Tests

`node --test remotion-composer/kit/test/engine.test.ts` (the engine's logic) and
`pytest tests/kit` (voice, staging, the engine's tests, and a small video built end to end).
