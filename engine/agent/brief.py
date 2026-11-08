"""Builds the unattended-run brief for one video job.

The template generalizes the two trial briefs that produced user-accepted
videos (2026-11-05): unattended preamble, delegated approvals, the locked
audio-first production rules, review gates, budget-with-graceful-degrade,
and the report/exports contract the runner collects afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# The one image model for every generated image (owner's choice, 2026-11-07): as real-looking as
# Sunburst and about three times faster (10 s against 34 s for a high 1024x1536 portrait).
IMAGE_MODEL = "gpt-image-2.6-flare"
IMAGE_QUALITY = "high"


def _references_section(references: list[dict[str, Any]]) -> str:
    if not references:
        return ""
    lines = ["## Reference files (use these; never regenerate what a reference already provides)", ""]
    for ref in references:
        role = ref.get("role") or "reference"
        label = f" - {ref['label']}" if ref.get("label") else ""
        lines.append(f"- {ref['path']} - role: {role}{label}")
    return "\n".join(lines) + "\n"


def build_brief(
    *,
    prompt: str,
    references: list[dict[str, Any]],
    project_dir: Path,
    budget_usd: float,
    duration_seconds: float = 30,
    language: str = "en",
    captions: bool = True,
    music: bool = True,
) -> str:
    project = str(project_dir)
    caption_rule = (
        "5. Word-level captions, burned in; they must match the spoken words exactly and never "
        "collide with other on-screen text."
        if captions else
        "5. No captions (the client disabled them)."
    )
    music_rule = (
        "6. Background music is wanted: generate or reuse a bed, mix with `audio_mixer` full_mix, "
        "voice ducking on, -14 LUFS integrated."
        if music else
        "6. No background music (the client disabled it); still deliver -14 LUFS integrated."
    )
    return f"""# Unattended production run

You are running unattended (headless). There is no human available: never pause for approval and
never ask questions. Every approval and checkpoint is delegated to you - record your choices in the
standard decision log instead of asking. Work until the finished video is exported, then stop.

## The request

{prompt.strip()}

Target: vertical 9:16, 1080x1920, about {duration_seconds:g} seconds, language: {language}.

{_references_section(references)}
## Locked production decisions (do not revisit; record them as given)

1. Standard OpenMontage flow: read AGENT_GUIDE.md first and follow its routing for this request.
2. If the video has a speaking presenter: audio first, ONE voice for the whole video. Generate all
   spoken lines with `fal_elevenlabs_tts` (model_id eleven-v3; pick one fitting voice and keep it
   for every line). Pass raw TTS through silence trimming (`trim_silence: true` where the tool
   supports it); never hand-roll audio filters.
3. Every on-camera talking shot comes from tool `fal_avatar` with `model: "h3max-acting"` - read its
   skill in `.agents/skills/fal-avatar/` before the first call and follow its audio contract and
   prompt rules. Never use a video model's own generated speech; never add a second voice. The
   delivered take's audio is used verbatim in the mix.
4. The hook matters: the first frame must work muted (a scroller decides in about one second).
{caption_rule}
{music_rule}
7. Render with Remotion and deliver BT.709 limited range, H.264, 1080x1920.
8. Every generated image (presenter portraits, product shots, scenes, cards) comes from tool
   `openai_image` with `model: "{IMAGE_MODEL}"` and `quality: "{IMAGE_QUALITY}"`, size `1024x1536`
   for vertical frames. Never another image tool or model, FLUX included. Describe a real phone
   photo: natural skin texture, real light, nothing retouched. Show only the client's own brand:
   no other real brand names, logos or labels, and leave a label blank when the brief doesn't
   give the brand (the model otherwise invents one, or uses a real one).

## Quality bar

- Review every generated take before using it: extract frames at the start and middle, check the
  person's identity holds and the mouth moves during speech, and probe the audio level - a take
  whose audio peaks near zero is broken. Reshoot a bad take at most once; prefer a reshoot over
  keeping a bad take.
- No overlapping or duplicated on-screen text; music must never drown the voice.

## Progress notes

Nobody can answer you, but the client follows your messages live while the video is made. Before
each step, write one short sentence on what you are doing or what you just found, in plain words
about the video (the script, the presenter, the voice, the shots, the music, the captions, the
edit). Leave file names, tool and model names, commands and costs out of these sentences.

## Budget, limits, reporting

- Hard cap: ${budget_usd:.2f} total paid generation for this run. Estimate before every paid call
  and keep the standard cost log accurate (write it to `{project}/artifacts/cost_log.json` as a JSON
  list of {{"item", "usd"}} entries). If the remaining budget cannot cover a step, ship the best
  video possible without it instead of failing.
- Do not modify repository code, do not run git commit/push, do not touch files outside this
  repository, the project directory below, and the reference files listed above.
- Run tools with `python`: its interpreter already has every dependency (there is no venv to look
  for). The project directory below and /tmp are the only places you can write.
- All project work goes in: {project}
- When finished, write {project}/RUN_REPORT.md: what you produced, every paid call with its cost,
  the total spend, rough wall time per stage, problems you hit and how you solved them, and the
  absolute path of the final video. The final MP4 must exist under {project}/exports/ before you
  stop.
"""


def build_revise_prompt(*, feedback: str, project_dir: Path, budget_usd: float) -> str:
    project = str(project_dir)
    return f"""The client reviewed the video you produced in this session and wants a revision. You are again
running unattended: never pause for approval, decide and record. Their feedback:

{feedback.strip()}

Apply the feedback with the smallest change that honors it: reuse every asset that is still right,
regenerate only what the feedback touches, and keep every locked production decision from the
original brief (one voice, fal_avatar h3max-acting for on-camera speech, {IMAGE_MODEL} at
{IMAGE_QUALITY} quality for any new image, review gates). Additional
paid-generation budget for this revision: ${budget_usd:.2f}; keep logging costs to
{project}/artifacts/cost_log.json.

Export the revised video as a NEW file under {project}/exports/ (do not overwrite the previous
export) and update {project}/RUN_REPORT.md with a "Revision" section before you stop.
"""
