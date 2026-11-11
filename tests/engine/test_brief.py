"""The unattended brief: what every video job's agent is told before it starts."""

from __future__ import annotations

from pathlib import Path

from engine.agent.brief import build_brief, presenter_card


def _brief(job: str = "job_x", **overrides) -> str:
    args = dict(prompt="An ad for a cold brew", references=[],
                project_dir=Path(f"/data/projects/{job}"), budget_usd=3.0)
    args.update(overrides)
    return build_brief(**args)


def test_the_brief_asks_for_plain_progress_notes():
    # Clients show the agent's messages live as a progress feed; a run that only thinks and calls
    # tools leaves that feed empty for the whole production.
    notes = _brief().split("## Progress notes", 1)[1].split("\n## ", 1)[0]
    assert "Before\neach step, write one short sentence" in notes
    assert "file names, tool and model names, commands and costs" in notes
    # A real run opened its feed with "I'll start by reading the agent guide, as the project
    # instructions require": true, and nothing a client should read.
    assert "never speak of this brief, a guide or your own instructions" in " ".join(notes.split())


def test_the_brief_researches_a_named_product():
    # A promo for a real product must be made from the product's real site and assets. The run
    # that exposed this (job_269769c8a4970996) never fetched the named site and invented claims.
    brief = _brief()
    assert "WebFetch" in brief
    assert "invent\n   nothing" in brief or "invent nothing" in brief.replace("\n   ", " ")
    assert "a real screenshot beats a generated look-alike" in brief.replace("\n   ", " ")


def test_the_brief_carries_a_presenter_card_that_varies_by_job():
    # Unguided runs all describe the same stock presenter and the image model renders what reads
    # as the same woman in every video; the card forces variety across jobs, stably per job.
    assert presenter_card("job_a") == presenter_card("job_a")
    cards = {presenter_card(f"job_{n}") for n in range(12)}
    assert len(cards) >= 8
    brief = _brief("job_abc123")
    assert presenter_card("job_abc123") in brief.replace("\n   ", " ")


def test_the_brief_demands_motion_on_every_scene():
    # A 30 s promo shipped as four static stills; the brief now forbids the slideshow treatment
    # and names the priced image-to-video tool for scenes without an avatar take.
    brief = _brief().replace("\n   ", " ")
    assert "this is a video, not a slideshow" in brief
    assert "h3_max_video" in brief
    assert "image_to_video" in brief


def test_the_brief_demands_a_written_plan_before_paid_work():
    # A planned video comes out far better than an improvised one (owner, 2026-11-08), so the
    # director writes the full plan after research and before the first paid call, then executes
    # it at once - no approval pause in an unattended run.
    brief = _brief("job_plan")
    section = brief.split("## Plan first, then produce", 1)[1].split("\n## ", 1)[0]
    assert "/data/projects/job_plan/artifacts/video_plan.md" in section.replace("\\", "/")
    assert "BEFORE the first paid generation call" in section
    assert "needs no approval" in section
    assert "video_plan.md" in build_revise_prompt_text()


def build_revise_prompt_text() -> str:
    from engine.agent.brief import build_revise_prompt
    return build_revise_prompt(feedback="Tighten the hook", project_dir=Path("/data/projects/job_plan"),
                               budget_usd=1.2)


def test_every_picture_of_the_presenter_is_made_from_the_portrait():
    # Video 715 described its three cutaways in words alone, in the same batch as the portrait.
    # The image model invented another person each time (long braids beside a presenter with
    # short grey hair); the director bought all three again as "hands only" and shipped hands of
    # another skin tone. Video 714 had edited its cutaways from the portrait: right first time.
    brief = " ".join(_brief().split())
    assert "One presenter, one source picture: generate the presenter's portrait first" in brief
    assert "any part of them (their hands, an arm, a shoulder, their back, a view over their shoulder)" in brief
    assert '`generation_mode: "edit"` and `image_path` set to the portrait' in brief
    assert 'The same person as in the reference photo' in brief
    assert "In the burst these pictures wait for the portrait, as the takes do" in brief
    # The review names what to compare, hands included.
    quality = " ".join(_brief().split("## Quality bar", 1)[1].split("\n## ", 1)[0].split())
    assert "Hold every other shot that shows the presenter, or part of them, against the portrait" in quality
    assert "the skin tone (hands included)" in quality and "does not ship" in quality


def test_the_run_report_opens_with_a_summary_the_client_can_read():
    # A client app shows the run report as production notes after cutting every line that names
    # a tool, a model, a file or a cost. Video 714's report was written entirely in those, so
    # its client got no notes at all. The report now opens with a section written for them.
    brief = " ".join(_brief().split())
    assert "It opens with a section headed `## Summary`, and that section is the client's" in brief
    assert "one sentence each" in brief and "the plain words of your progress notes" in brief
    assert "No file or folder name, no tool, model or provider name, no command and no cost" in brief
    assert "Everything after it is for the operator" in brief
    assert "every paid call with its cost" in brief  # the operator's part is unchanged
    # The heading is quoted inside a sentence, never a heading of the brief itself.
    assert "\n## Summary" not in _brief()
    revise = " ".join(build_revise_prompt_text().split())
    assert 'add a "Revision" section' in revise
    assert "bring the `## Summary` section it opens with up to date" in revise
    assert "write one if the report has none" in revise


def test_the_brief_treats_the_budget_as_production_fuel():
    # The same run spent $0.99 of its $6.00 cap on static coverage.
    brief = _brief().replace("\n  ", " ")
    assert "production fuel, not a savings target" in brief
    assert "an under-delivery, not an economy" in brief


def test_the_voice_is_designed_for_the_presenter_in_one_call():
    # Until video 718 the director picked a stock voice by name, unchecked: a man in his sixties
    # could speak with a young woman's voice, and the stock voices end with 2026.
    brief = " ".join(_brief(language="es").split())
    rule = brief.split("4. If the video has a voice:", 1)[1].split("5. Every on-camera talking shot", 1)[0]
    assert "ONE voice for the whole video, made for this video's presenter" in rule
    assert "ONE call of tool `voiceover_tts`" in rule and "`.agents/skills/voice-design/`" in rule
    assert '`voice_description`, `gender` ("woman" or "man"), `age`' in rule and '`language` "es"' in rule
    assert "/data/projects/job_x/assets/audio/<line id>.mp3" in rule
    # The accent is the language's own unless the client asked, and no real person is imitated.
    assert "another accent only when the request asks for one" in rule
    assert "Never the name or the likeness of a real person" in rule
    # A voice that could not be kept (recorded in one piece, or a stock voice) is an answer the
    # director works with, and the client is not told of it.
    assert "it still gives every line in ONE voice (the designed voice recorded in one piece, or" in rule
    assert "a stock voice of the same gender and age" in rule and "keep it out of your notes and the summary" in rule
    assert "call it with just that line: it uses this video's voice" in rule
    assert "Then run `kit voice`" in rule
    # The single-line tool is not how a video's voice is made any more.
    assert "fal_elevenlabs_tts" not in brief and "eleven-v3" not in brief
    # The plan sets the two descriptions of one person next to each other.
    assert "the presenter and the voice description, side by side" in brief


def test_a_revision_speaks_a_changed_line_in_the_videos_own_voice():
    revise = " ".join(build_revise_prompt_text().split())
    assert "A spoken line that changes is spoken again with `voiceover_tts`, called with just that line" in revise
    assert "it uses the video's own voice" in revise
    # A voice that was removed with age, or was only ever a recording, is no reason to use another one.
    assert "If it answers that the voice no longer exists or cannot speak again, leave the spoken words as they are" in revise
    assert "never speak one line in another voice" in revise


def test_a_video_is_a_direct_ad_unless_the_client_chose_the_mention_style():
    # "promote" is the default and its brief is the one every video had before the choice existed.
    promote = _brief()
    assert _brief(style="promote") == promote and _brief(style="something else") == promote
    assert "The style of this video" not in promote
    assert "a deliberate closing beat under the CTA" in " ".join(promote.split())


def test_the_mention_style_sells_the_product_without_a_push():
    # The owner's words (2026-11-11): "The presenter still has to market what he describes ... he simply
    # describes the product and mentions it many times, as much as needed where needed, but he doesn't
    # force you / urge you to literally buy something or go to a site ... without using the usual
    # promotional wording (like 10% off or something) and not mentioning CTA stuff".
    brief = _brief(style="mention")
    section = " ".join(brief.split("## The style of this video: a mention (no call to action, no offer)", 1)[1]
                       .split("\n## ", 1)[0].split())
    # It is still a promotion of the product, said as often as it helps.
    assert "This video still sells" in section
    assert "names it as often as that helps, wherever it helps" in section
    assert "The product's NAME is said and shown freely" in section
    # No push to act, spoken or on screen, and no address to go to.
    assert "No call to action of any kind" in section and "in the script, on screen and in the post text" in section
    for push in ("click", "tap", "go to", "visit", "buy", "order", "sign up", "download", "try", '"link in bio"'):
        assert push in section, push
    assert "its web address is not shown or spoken as a place to go" in section
    # No deal talk.
    assert 'No offer wording: no discount, coupon code, deadline, "limited time" or price pushed as a deal' in section
    # No card for it either, and no softer push in its place.
    assert "No card that asks for an action and no end card" in section
    assert "do not put a softer version of the same push in their place" in section
    # The presenter stays a host: no customer story, no results of their own.
    assert "never a customer, and never someone reporting results of their own" in section
    # It comes before the locked decisions and outranks a stage skill's or a look's call to action.
    assert brief.index("## The style of this video") < brief.index("## Locked production decisions")
    assert "Where a stage skill or a kit look asks for a call to action, an offer line, a CTA card or an end card" in section
    # A brief pasted from an older ad may still carry an offer or a push: the style the client chose wins,
    # and the client is told.
    assert "If the request above itself names an offer or tells the viewer what to do, the style still holds" in section
    assert "leave that part out, and say so in one line of the summary" in section
    # The one rule of the brief that names the call to action no longer does.
    assert "under the CTA" not in brief and "a deliberate closing beat." in " ".join(brief.split())
    assert "\u2014" not in brief and "\u2013" not in brief


def test_every_video_is_made_with_its_cover_and_its_post_text():
    brief = _brief(language="de")
    section = " ".join(brief.split("## What is posted with the video", 1)[1].split("\n## ", 1)[0].split())
    # The cover is a frame of the finished video, chosen from the sheet and looked at.
    assert "`kit cover --at <second>` saves that frame of the finished video" in section
    assert "/data/projects/job_x/exports/cover.jpg" in section.replace("\\", "/")
    assert "Look at the picture once" in section
    # The post text: three fields, in the video's language, with limits a short-video post can take.
    assert "/data/projects/job_x/artifacts/post.json" in section.replace("\\", "/")
    assert '`{"title": "...", "description": "...", "hashtags": ["...", "..."]}`' in section
    assert "language (de)" in section and "at most 90 characters" in section and "at most 300 characters" in section
    assert "four to six, each without the `#` and without spaces" in section
    assert "Nothing in it that the site and the request do not back up" in section
    assert "it may end on the video's call to action" in section
    # Both are part of what must exist before the director stops.
    assert "and so must the cover picture and the post text" in " ".join(brief.split())
    # A mention's post text has no push either.
    mention = " ".join(_brief(style="mention").split())
    assert "like the video, it carries no call to action and no offer wording" in mention
    assert "it may end on the video's call to action" not in mention


def test_a_revision_keeps_the_style_and_brings_the_cover_and_the_post_text_up_to_date():
    from engine.agent.brief import build_post_prompt, build_revise_prompt

    args = dict(feedback="Tighten the hook", project_dir=Path("/data/projects/job_plan"), budget_usd=1.2)
    revise = " ".join(build_revise_prompt(**args).split())
    assert "mention style" not in revise
    assert "make the cover again from the new export (`kit cover --at <second>`)" in revise
    assert "post.json (the post's title, description and hashtags) up to date" in revise
    mention = " ".join(build_revise_prompt(**args, style="mention").split())
    assert "This video is in the mention style" in mention
    assert "no call to action, no offer wording and no end card" in mention
    assert "The revision keeps that, unless the feedback itself asks for one" in mention
    # The one follow-up of a finished video that has no post text: the same rules, nothing else touched.
    follow = build_post_prompt(project_dir=Path("/data/projects/job_plan"), language="fr", style="mention")
    assert "change nothing else in the project, and stop" in follow and "language (fr)" in follow
    assert "/data/projects/job_plan/artifacts/post.json" in follow.replace("\\", "/")
    assert "no call to action and no offer wording" in follow and '"hashtags"' in follow


def test_the_voice_skill_says_what_the_tool_needs_and_what_never_to_do():
    from tools.audio.voiceover_tts import VoiceoverTTS

    assert VoiceoverTTS.agent_skills == ["voice-design"]
    skill = (Path(__file__).resolve().parents[2] / ".agents" / "skills" / "voice-design" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: voice-design\n")
    for words in ("`voiceover_tts`", '"voice_description"', '"gender"', '"age"', '"lines"', "`new_voice: true`",
                  "Another accent ONLY when the request asks for it", "Never name a real person",
                  "the video's voice no longer exists", "Do not speak one line in another voice",
                  "do not mention it to the client", "`recording`", "cannot speak again"):
        assert words in skill, words
    assert "\u2014" not in skill and "\u2013" not in skill
    # Every input the skill shows is one the tool takes.
    properties = VoiceoverTTS.input_schema["properties"]
    assert {"voice_description", "gender", "age", "language", "lines", "model", "new_voice"} <= set(properties)
