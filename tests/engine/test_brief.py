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
