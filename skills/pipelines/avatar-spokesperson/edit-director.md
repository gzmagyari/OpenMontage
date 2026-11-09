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

## Headless Jobs: Your Decisions Are the Edit (Binding)

In a boxed/headless job the edit decisions - cut order, overlay timing, CTA landing,
what carries each beat - are yours and live in ONE place: `composition/edit.json`.
There is no composition code to draft or to review: the video kit renders the edit.
When the real timings land (`kit voice`, right after the lines are spoken), cuts pinned
to words move by themselves; run `kit check` and change only what it reports. Judge the
edit against the footage on a sheet (`kit sheet`), never by rendering stills one at a
time, and never by writing sequencing or caption code - that is the serial path this
pipeline exists to avoid.

## Common Pitfalls

- Overcutting to simulate energy.
- Letting captions, side panels, and lower thirds compete for the same area.
- Ending without a clean CTA hold.
