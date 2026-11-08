"""Builds the unattended-run brief for one video job.

The template generalizes the two trial briefs that produced user-accepted
videos (2026-11-05): unattended preamble, delegated approvals, the locked
audio-first production rules, review gates, budget-with-graceful-degrade,
and the report/exports contract the runner collects afterwards.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

# The one image model for every generated image (owner's choice, 2026-11-07): as real-looking as
# Sunburst and about three times faster (10 s against 34 s for a high 1024x1536 portrait).
IMAGE_MODEL = "gpt-image-2.6-flare"
IMAGE_QUALITY = "high"

# One suggested presenter per run, picked from the job id. Left to its own defaults, every run
# describes the same person (five November 2026 runs all wrote a late-20s woman, shoulder-length
# brown hair, hoop earrings, window light) and the image model renders what reads as the same
# woman in every video. The card varies with the job; the director adapts it to the product.
_AGES = ["early 20s", "mid 20s", "early 30s", "late 30s", "mid 40s", "early 50s", "60s"]
_GENDERS = [("woman", "her"), ("man", "his")]
_HERITAGES = ["Black", "East Asian", "South Asian", "Latin American", "Middle Eastern", "white",
              "Southeast Asian", "mixed-heritage"]
_HAIR = ["short cropped hair", "curly shoulder-length hair", "long hair tied back", "a buzz cut",
         "a neat afro", "salt-and-pepper short hair", "a loose bun", "box braids",
         "slicked-back dark hair", "a short grey crop"]
_WARDROBE = ["a denim jacket over a white tee", "a plain black t-shirt", "a checked flannel shirt",
             "a grey hoodie", "a navy blazer over a t-shirt", "a chunky knitted jumper",
             "an open-collar linen shirt", "a work apron over a henley", "a retro track jacket",
             "a dark turtleneck"]
_SETTINGS = ["in a bright kitchen", "on an evening city street",
             "in a home office lit by a warm desk lamp", "on a sunlit balcony",
             "at a workshop bench", "in the driver's seat of a parked car in daylight",
             "at a gym entrance", "on a leafy park path", "in a quiet cafe corner",
             "at a tidy studio desk in soft light"]


def presenter_card(job_key: str) -> str:
    """A concrete presenter suggestion: stable for one job, different across jobs."""
    digest = hashlib.sha256(job_key.encode("utf-8")).digest()

    def pick(options: list, index: int):
        return options[digest[index] % len(options)]

    gender, pronoun = pick(_GENDERS, 0)
    return (f"a {pick(_HERITAGES, 1)} {gender} in {pronoun} {pick(_AGES, 2)} with {pick(_HAIR, 3)}, "
            f"wearing {pick(_WARDROBE, 4)}, filmed {pick(_SETTINGS, 5)}")


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
    card = presenter_card(project_dir.name or project)
    caption_rule = (
        "8. Word-level captions, burned in; they must match the spoken words exactly and never "
        "collide with other on-screen text."
        if captions else
        "8. No captions (the client disabled them)."
    )
    music_rule = (
        "9. Background music is wanted: generate or reuse a bed, mix with `audio_mixer` full_mix, "
        "voice ducking on, -14 LUFS integrated."
        if music else
        "9. No background music (the client disabled it); still deliver -14 LUFS integrated."
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
2. If the request names a product, company, app or website: research it before writing a word of
   script. Fetch its pages with WebFetch (WebSearch first if the address is unclear) and keep
   every claim, feature and price in the video true to what the site really says - invent
   nothing. Save 2-4 of its real images or screenshots (download with python; the proxy is
   already configured) under {project}/refs/ and use them wherever the video shows the product
   or its results: a real screenshot beats a generated look-alike. If the site cannot be
   reached, note it in a progress note and stay with claims so generic they cannot be wrong.
3. A promotional or product video has an on-camera presenter delivering the key lines unless the
   request says otherwise. The presenter for THIS run, unless the request or the product plainly
   needs someone else: {card}. Keep that exact person consistent in every shot, and never fall
   back to the stock look-alike presenter (young woman, shoulder-length brown hair, hoop
   earrings, window light) that unguided runs default to.
4. If the video has a speaking presenter: audio first, ONE voice for the whole video. Generate all
   spoken lines with `fal_elevenlabs_tts` (model_id eleven-v3; pick one fitting voice and keep it
   for every line). Pass raw TTS through silence trimming (`trim_silence: true` where the tool
   supports it); never hand-roll audio filters.
5. Every on-camera talking shot comes from tool `fal_avatar` with `model: "h3max-acting"` - read its
   skill in `.agents/skills/fal-avatar/` before the first call and follow its audio contract and
   prompt rules. Never use a video model's own generated speech; never add a second voice. The
   delivered take's audio is used verbatim in the mix.
6. Every scene moves - this is a video, not a slideshow. A scene not covered by an avatar take is
   animated from its still with tool `h3_max_video` (model `h3-max`, operation image_to_video,
   about $0.035 per output second; skill in `.agents/skills/minimax-h3/`): generate the still,
   then give it real subject and camera motion with a clear motion prompt. A slow pan or zoom
   over a static image is a last resort for when the budget is already spent, never the plan,
   and a fully static frame is allowed only as a deliberate closing beat under the CTA.
7. The hook matters: the first frame must work muted (a scroller decides in about one second).
{caption_rule}
{music_rule}
10. Render with Remotion and deliver BT.709 limited range, H.264, 1080x1920.
11. Every generated image (presenter portraits, product shots, scenes, cards) comes from tool
    `openai_image` with `model: "{IMAGE_MODEL}"` and `quality: "{IMAGE_QUALITY}"`, size `1024x1536`
    for vertical frames. Never another image tool or model, FLUX included. Describe a real phone
    photo: natural skin texture, real light, nothing retouched. Show only the client's own brand:
    no other real brand names, logos or labels, and leave a label blank when the brief doesn't
    give the brand (the model otherwise invents one, or uses a real one). Real photos and
    screenshots downloaded from the client's own site are references, not generated images: use
    them as they are.

## Plan first, then produce

Gather everything the plan needs first (the request, the researched site and its downloaded real
assets, the reference files) - then write the complete video plan to
`{project}/artifacts/video_plan.md` BEFORE the first paid generation call. The plan states: the
angle and the muted-viewable hook (what the first frame shows and why it stops a scroller); the
exact script with second marks; a numbered shot list in which every shot names what is on screen,
where its visual comes from (avatar take, animated still, real site screenshot) and how it moves;
the presenter and the voice; the music direction; every piece of on-screen text; and the expected
paid calls. Post one progress note giving the plan in a sentence or two, then start producing it
immediately - the plan needs no approval, and production follows it shot by shot. If production
forces a change, update the plan file and record the reason in the decision log and RUN_REPORT.md.

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
- The budget is production fuel, not a savings target: plan the shot list to put most of it on
  screen (avatar takes, animated scenes, real assets). Shipping mostly static coverage with most
  of the budget unspent is an under-delivery, not an economy.
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

Read {project}/artifacts/video_plan.md first to see what the video was built to be, then apply the
feedback with the smallest change that honors it: reuse every asset that is still right, regenerate
only what the feedback touches, and keep every locked production decision from the original brief
(one voice, fal_avatar h3max-acting for on-camera speech, h3_max_video motion on scenes without a
take, {IMAGE_MODEL} at {IMAGE_QUALITY} quality for any new image, review gates). Update the plan
file where the revision changes it.
Additional paid-generation budget for this revision: ${budget_usd:.2f}; keep logging costs to
{project}/artifacts/cost_log.json.

Export the revised video as a NEW file under {project}/exports/ (do not overwrite the previous
export) and update {project}/RUN_REPORT.md with a "Revision" section before you stop.
"""
