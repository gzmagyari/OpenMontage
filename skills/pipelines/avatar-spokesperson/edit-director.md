# Edit Director - Avatar Spokesperson Pipeline

## When To Use

Turn the planned presenter scenes and produced assets into a coherent spokesperson timeline. The quality bar is steady delivery, readable support layers, and a clear CTA landing.

## Process

### 1. Cut The Presenter Track First

Assemble the core spokesperson performance before adding support layers. If the presenter cut is weak, extra graphics will not rescue it.

### 2. Add Support Layers Sparingly

Use overlays only where they help:

- short proof points,
- product names,
- pricing or feature cards,
- CTA reinforcement,
- subtitles.

### 3. Respect Spoken Rhythm

Keep pauses where they help emphasis. Do not trim so tightly that the avatar feels rushed or robotic.

### 4. Plan Deliverables Clearly

Recommended metadata keys:

- `hero_cut_order`
- `cta_frame_range`
- `overlay_timing_map`
- `variant_decisions`

### 5. Quality Gate

- the presenter remains the anchor,
- overlays are timed cleanly,
- scene transitions are calm and intentional,
- the CTA lands once and clearly.

## Headless Jobs: Your Decisions, the Composer's Draft (Binding)

In a boxed/headless job the edit decisions - cut order, overlay timing, CTA landing,
what carries each beat - are yours and stay yours. Their IMPLEMENTATION in Remotion
code arrives as the `composer` subagent's first full draft (spawned at script lock
with the plan; AGENT_GUIDE -> "Delegation (Binding)"). Reconcile your edit decisions
against that draft when the real asset timings land (TTS/caption durations shift
seconds around): small fixes you make directly, structural rework goes back to the
composer as a fix list. Do not rewrite the composition from scratch solo - that is
the serial path this pipeline exists to avoid.

## Common Pitfalls

- Overcutting to simulate energy.
- Letting captions, side panels, and lower thirds compete for the same area.
- Ending without a clean CTA hold.
