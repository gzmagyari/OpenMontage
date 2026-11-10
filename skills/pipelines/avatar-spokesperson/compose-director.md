# Compose Director - Avatar Spokesperson Pipeline

## When To Use

Render the final spokesperson outputs. The bar is simple: the presenter must look stable, speech must be clear, and subtitles or support cards must not crowd the frame.

## Runtime Routing (HARD CONSTRAINT — Remotion only)

Phase 1 deferred from HyperFrames. `edit_decisions.render_runtime` must be `"remotion"`. This pipeline depends on the Remotion `TalkingHead` composition and `remotion_caption_burn` — both have no HyperFrames parity in Phase 1.

- If `edit_decisions.render_runtime == "hyperframes"`, stop. Re-open the idea stage and surface the constraint. Silent rewrite is a governance violation.
- Per AGENT_GUIDE.md → "Present Both Composition Runtimes (HARD RULE)": the lock to remotion is NOT an excuse to skip the conversation. The user deserves to know that HyperFrames exists as a runtime and why it isn't viable for avatar-spokesperson. Log a `render_runtime_selection` decision with hyperframes `rejected_because: "TalkingHead + caption parity deferred on avatar-spokesperson"`.
- Pass `proposal_packet`/`brief` to `video_compose.execute()` for in-tool runtime-swap detection.

In a headless job the video kit drives Remotion (`kit sheet`, `kit render`): `TalkingHead`,
`remotion_caption_burn` and `video_compose` belong to the interactive path and are not used.

## Prerequisites

| Layer | Resource | Purpose |
|-------|----------|---------|
| Schema | `schemas/artifacts/render_report.schema.json` | Artifact validation |
| Prior artifacts | `state.artifacts["edit"]["edit_decisions"]`, `state.artifacts["assets"]["asset_manifest"]` | What to render |
| Tools | `video_compose`, `audio_mixer`, `video_stitch`, `audio_enhance` | Render and audio finishing |
| Playbook | Active style playbook | Typography and layout rules |

## Process

### 1. Render The Hero Cut First

Prefer one strong master before derivatives. Compose:

- presenter video,
- subtitles,
- lower-thirds,
- CTA cards,
- mixed narration.

### 2. Keep The Frame Clean

Subtitle and CTA placement matter more here than flashy transitions. Leave the face and mouth region unobstructed.

### 3. Verify Mouth Timing And Audio

If the avatar path used lip sync or audio-driven talking head, check:

- mouth timing,
- face artifacts,
- drift on long sections,
- audio clarity.

### 4. Verify Every Output

Record important findings in:

- `render_report.verification_notes`
- `render_report.warnings`
- `render_report.metadata.variant_notes`

### 5. Quality Gate

- the output file is valid,
- speech is clear,
- subtitles stay readable,
- the presenter remains visually stable.

## Headless Jobs: Check, Sheet, Render (Binding)

- The video is rendered by the kit from `composition/edit.json`, the job's restyled look
  and any custom scenes. The order: `kit mix` (the soundtrack), `kit check` (no errors,
  nothing missing), `kit sheet` - LOOK at the sheet and fix what it shows in the edit or
  the look - then `kit render --name <name>`. One render: a second one needs a defect QC
  can name. Never run Remotion by hand.
- The compose checkpoint carries `metadata.kit` (checkpoint-protocol -> Boxed-Job
  Efficiency): the look's starter and what was restyled, the custom scenes, how many
  sheets and renders it took. A compose checkpoint without a passing `kit check` is
  INVALID; when a subagent was used it also carries the delegation record.
- The final render may run detached while you do LLM-side work (QC notes, RUN_REPORT
  drafting):
  `nohup sh -c 'kit render --name <name>; echo "RENDER EXIT $?"' > work/render.out 2>&1 &`.
  Collect it ONCE, in one waiting call, never by polling:
  `until grep -q "RENDER EXIT" work/render.out; do sleep 2; done; cat work/render.out`
  - and read what it printed: `kit render` verifies the export itself, and an exit
  other than 0 means there is no video yet. The Bash tool's own background mode is
  switched off in jobs. The final export, its verification and settlement stay yours,
  never a subagent's.

## Common Pitfalls

- Letting subtitles cover the chin or mouth area.
- Shipping a long lip-sync render without spot-checking drift.
- Making derivative crops that cut off the presenter or CTA.
