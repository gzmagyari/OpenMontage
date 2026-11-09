# Asset Director - Avatar Spokesperson Pipeline

## When To Use

This stage prepares the actual spokesperson ingredients: narration, avatar or lip-sync footage, subtitle assets, branded backgrounds, and the minimal support graphics needed to complete the cut.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/asset_manifest.schema.json` | Artifact validation |
| Prior artifacts | `state.artifacts["scene_plan"]["scene_plan"]`, `state.artifacts["script"]["script"]`, `state.artifacts["idea"]["brief"]` | Presenter plan and narration needs |
| Tools | `talking_head`, `lip_sync`, `tts_selector`, `subtitle_gen`, `image_selector`, `audio_enhance` — selectors auto-discover all available providers from the registry | Avatar, narration, and support asset options |
| Playbook | Active style playbook | Background, type, and subtitle rules |

## Process

### 1. Lock The Avatar Generation Path

Use one primary path and record it clearly:

- `talking_head` from still image plus audio,
- `lip_sync` from existing presenter plate plus new audio,
- externally supplied avatar render if created outside the current runtime.

Do not hide a blocked avatar path. Record it.

### 1a. Take Model Economics (Binding)

Standard presenter takes use `fal_avatar` `model: "h3max"` - one audio-conditioned acting
generation whose native lips ship, trimmed to the line with the verbatim voice ($0.056/s at
768P). `"h3max-acting"` (adds the frame-accurate lip-sync pass, ~$0.26/s) is for tight
close-ups and lip-critical shots only. When QC rejects a take's LIPS specifically, upgrade
that take in place: call `fal_avatar` with the take as `video_path` plus its line's audio
(the lip-fix mode, +$0.20/s) instead of regenerating the whole take.

Every take is generated from its PREPARED audio: the file `kit voice` lists for that part
(`assets/audio/voice_<part>.wav`), with `trim_silence` off, saved at the take path listed
beside it. The word timings every cut and caption hangs on belong to that exact file; a take
made from the raw line, or trimmed again, drifts out of sync.

### 1b. Sample Preview (Prevents Wasted Spend)

Before batch-generating assets, produce one sample of each expensive type and show the user:

1. **TTS sample** (if generating narration): Generate one section. Confirm voice, pace, and persona before batching the rest.
2. **Avatar sample** (if using `talking_head`): Generate a short test clip. Confirm the avatar quality is acceptable before committing to full generation.

If rejected, adjust parameters and retry (max 3 iterations). Do not batch until approved.

### 2. Resolve Narration Before Support Graphics

Spokesperson videos depend on speech. Determine whether narration is:

- supplied,
- TTS-generated,
- already embedded in a presenter plate.

If narration is missing and no TTS tool is available, mark the project blocked instead of pretending the stage succeeded.

### 3. Build The Minimal Support Kit

Prepare only what the scene plan actually needs:

- subtitle files,
- one lower-third system,
- CTA card,
- background or plate assets,
- optional still or product support images.

### 4. Use Metadata For Capability Truth

Recommended metadata keys:

- `avatar_generation_path`
- `narration_assets`
- `subtitle_assets`
- `background_assets`
- `scene_asset_index`
- `blocked_assets`

### 5. Quality Gate

- the avatar path is explicit,
- narration and avatar assets align,
- support graphics stay minimal,
- every referenced file exists.

## Parallel Generation (Binding)

When this stage needs several independent paid generations - presenter takes, b-roll
motion clips, scene stills - submit them ALL from ONE Bash call that runs the
submissions concurrently. The harness executes tool calls one after another even when
you issue several in a single message, so sequential submission serializes provider
waits: three ~90-second takes become 4-5 minutes instead of ~90 seconds.

Pattern (takes shown; the same shape fits any independent batch):

```bash
python - <<'PY'
import json, sys
sys.path.insert(0, "/app")
from concurrent.futures import ThreadPoolExecutor
from tools.tool_registry import registry
registry.discover()
tool = registry._tools["fal_avatar"]
BASE = "/data/projects/<project>"
takes = {
    "avatar1": {"model": "h3max", "image_path": ..., "audio_path": ..., ...},
    "avatar2": {...},
    "avatar3": {...},
}
def run(item):
    name, params = item
    result = tool.execute(params)
    json.dump(result, open(f"{BASE}/artifacts/{name}_call.json", "w"), indent=1)
    return name, result
with ThreadPoolExecutor(max_workers=len(takes)) as pool:
    results = dict(pool.map(run, takes.items()))
# Bookkeeping rides in the SAME call (checkpoint-protocol -> Boxed-Job Efficiency):
log_path = f"{BASE}/artifacts/cost_log.json"
log = json.load(open(log_path))
for name, r in results.items():
    print(name, "->", r.get("output_path"), r.get("cost_usd"))
    log.append({"item": f"{name} H3 Max avatar take", "usd": r.get("cost_usd")})
json.dump(log, open(log_path, "w"), indent=1)
PY
```

Rules:

- One Bash call per batch; the concurrency lives inside it (ThreadPoolExecutor, or
  `&` + `wait` over separate processes writing per-item logs).
- Write each submission's full response JSON to `artifacts/<name>_call.json` exactly as
  you would for a sequential call, and append the cost-log entries in the same call.
- Inspection stays separate and UNCHANGED: after the batch returns, verify every result
  (start/middle frames, durations, same presenter) before using any of them.
- Samples still come first where the process requires them; the parallel batch is what
  follows an approved sample. Never wait for one take to finish before SUBMITTING the
  next.

### Background burst (stage overlap)

At script lock, launch the batch above with `run_in_background` instead of waiting on
it: one background Bash call for the whole burst, chained inside - TTS first
(concurrent, each line saved at `assets/audio/<line id>.mp3`), then `kit voice`, then
takes / b-roll / music / image concurrently as their inputs land, each writing its
result JSON and cost-log entries exactly as above. While it cooks, restyle the look
and settle the layout against placeholders with `kit sheet --draft`; collect the
results at edit time.

Inside the burst, the takes to film come from `kit voice`, not from the script: it may have
split a long line into two takes, and it names the prepared audio and the take path of each.

```python
subprocess.run(["kit", "voice"], check=True, cwd=BASE)          # after every line is spoken
voice = json.load(open(f"{BASE}/work/kit/voice.json"))
takes = {part["id"]: {"model": "h3max", "image_path": PORTRAIT, "prompt": PROMPT,
                      "audio_path": f"{BASE}/{part['audio']}",   # the prepared file, as it is
                      "output_path": f"{BASE}/{part['take']}"}
         for line in voice["lines"] for part in line["parts"] if part.get("take")}
```

Before RUN_REPORT.md: every background task completed, every
expected output file exists; resubmit only the failed items. Never end the session
with a background job still running. Never poll it either: launch, work, collect ONCE
at the stage boundary - repeated status-check calls are wasted turns.

### Shopping list (Binding)

The burst buys exactly what the locked plan's shopping list names (script-director ->
"Script Lock in Headless Jobs"), writing each item to its listed artifact path - the
edit names the same paths, so `kit sheet --draft` shows a placeholder until each file
lands. Failed items are
resubmitted idempotently (same per-item files). A paid item NOT on the list may be
bought mid-run only after appending a one-line justification to the plan file
(`artifacts/video_plan.md`) in the same call that buys it - QC-driven re-takes are the
expected case, silent extras are not.

## No-Avatar Path

When the EP has triggered a narration-over-graphics pivot (neither `talking_head` nor `lip_sync` available), skip avatar generation entirely and produce a graphics-driven asset kit instead:

### What to produce:
1. **Narration audio** — via `tts_selector` (mandatory; block the project if no TTS is available either).
2. **Scene visuals** — via `image_selector` or `video_selector`. One primary visual per scene that reinforces the spoken point (diagram, illustration, product shot, or stock footage).
3. **Subtitle files** — same as standard path.
4. **Text cards** — key-point overlays, stat cards, CTA end card.
5. **Backgrounds** — consistent family matching the playbook.

### What to skip:
- No `talking_head` or `lip_sync` calls.
- No presenter framing metadata.
- `avatar_generation_path` should be set to `"none — narration-over-graphics pivot"`.

### Metadata for this path:
- `avatar_generation_path`: `"narration_over_graphics"`
- `pivot_reason`: why the no-avatar path was chosen
- All other metadata keys remain the same.

### Mid-Production Fact Verification

If you encounter uncertainty during asset generation:
- Use `web_search` to verify visual accuracy of subjects (e.g. what does this building actually look like?)
- Use `web_search` to find reference images before generating illustrations
- Log verification in the decision log: `category="visual_accuracy_check"`

Visual accuracy matters. If the script mentions a specific place, person, or object,
verify what it actually looks like before generating images. Don't rely on
the AI model's training data — it may be wrong or outdated.

## Common Pitfalls

- Building decorative assets before the narration path is solved.
- Mixing multiple avatar-generation strategies in one simple spokesperson video.
- Marking the stage complete when the core presenter asset is still hypothetical.
- (No-avatar path) Generating filler visuals with no connection to the narration — every image must reinforce the spoken point.


## When You Do Not Know How

If you encounter a generation technique, provider behavior, or prompting pattern you are unsure about:

1. **Search the web** for current best practices — models and APIs change frequently, and the agent's training data may be stale
2. **Check `.agents/skills/`** for existing Layer 3 knowledge (provider-specific prompting guides, API patterns)
3. **If neither helps**, write a project-scoped skill at `projects/<project-name>/skills/<name>.md` documenting what you learned
4. **Reference source URLs** in the skill so the knowledge is traceable
5. **Log it** in the decision log: `category: "capability_extension"`, `subject: "learned technique: <name>"`

This is especially important for:
- **Video generation prompting** — models respond to specific vocabularies that change with each version
- **Image model parameters** — optimal settings for FLUX, GPT Image, Imagen differ and evolve
- **Audio provider quirks** — voice cloning, music generation, and TTS each have model-specific best practices
- **Remotion component patterns** — new composition techniques emerge as the framework evolves

Do not rely on stale knowledge. When in doubt, search first.

---

## Gate Reminder (Binding)

This stage gates on human approval (`human_approval_default: true`). After review passes:
checkpoint with `status="awaiting_human"`, present the summary (the Backlot board renders
the artifact), and **END YOUR TURN**. Do not start the next stage in the same response.
Approval is per-gate — an earlier "go ahead" does not cover this gate.

## HeyGen Avatar V

Use `heygen_avatar` for explicitly approved HeyGen digital twins. Inspect the look before rendering: Avatar V requires `digital_twin` and `avatar_v` in `supported_api_engines`. Pass script plus voice_id, or one supplied audio source. Keep the returned resume_job/job_path if polling times out; resume the job instead of submitting again. An ineligible look is a blocker, never permission to switch to Avatar IV. Read the provider-model-refresh skill.
