# Publish Director - Avatar Spokesperson Pipeline

## When To Use

Package the finished spokesperson outputs for delivery. This stage should make it obvious which file is the hero cut, which are derivatives, and what message or audience each version serves.

## Process

### 1. Label Deliverables Clearly

Distinguish:

- hero cut,
- vertical cutdown,
- square cutdown,
- language variants,
- watermark or review versions.

### 2. Keep Metadata Message-Led

Recommended metadata keys:

- `audience_segment`
- `cta_copy`
- `offer_name`
- `locale`
- `thumbnail_concept`

### 3. Package Review Notes

If the avatar path has limitations such as visible lip-sync risk, retain that note in the package instead of hiding it.

### 4. Quality Gate

- exports are clearly named,
- metadata matches the intended message,
- poster frame or thumbnail concept features the presenter cleanly,
- review notes stay attached to the package.

## Headless Jobs: Report Requirements (Binding)

RUN_REPORT.md opens with `## Summary`, the client's section: four to eight lines, one
sentence each, about the video in the plain words of the progress notes. No file, tool,
model or provider name, no command and no cost goes in it; a line that carries one is cut
before the client reads the notes, and a summary made only of such lines leaves the client
with no notes at all.

The rest of the report is the operator's. It is not complete without the kit record (the
look's starter and how it was restyled, custom scenes, sheets and renders), the delegation
record when a subagent was
used - which subagent produced which artifacts - and the shopping-list reconciliation
(every paid item on the list bought or accounted for; every bought item on the list
or justified in the plan file). Internal cost figures stay internal: customer-facing
notes never show cost, margin or multipliers.

## Common Pitfalls

- Mixing hero and derivative exports without clear naming.
- Reusing generic metadata that ignores the spokesperson offer.
- Dropping risk notes that matter for downstream publishing teams.

---

## Gate Reminder (Binding)

This stage gates on human approval (`human_approval_default: true`). After review passes:
checkpoint with `status="awaiting_human"`, present the summary (the Backlot board renders
the artifact), and **END YOUR TURN**. Do not start the next stage in the same response.
Approval is per-gate — an earlier "go ahead" does not cover this gate.
