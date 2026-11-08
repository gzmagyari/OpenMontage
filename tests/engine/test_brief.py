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


def test_the_brief_treats_the_budget_as_production_fuel():
    # The same run spent $0.99 of its $6.00 cap on static coverage.
    brief = _brief().replace("\n  ", " ")
    assert "production fuel, not a savings target" in brief
    assert "an under-delivery, not an economy" in brief
