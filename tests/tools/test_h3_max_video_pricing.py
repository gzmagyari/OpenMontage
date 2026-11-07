"""H3 Max is priced from fal's per-second rates, so a paid job can bill it and hold it to the budget."""

from __future__ import annotations

import pytest

import tools.fal_media as fal_media
from lib import job_ledger
from tools.provider_pricing import PriceQuoteRequired
from tools.video.h3_max_video import H3MaxVideo
from tools.video.wan_fal_video import WanFalVideo


@pytest.fixture()
def fake_fal(monkeypatch):
    """fal's queue answering one finished 5.4 s clip."""
    monkeypatch.setenv("FAL_KEY", "test")
    replies = {
        "POST": {"request_id": "r1", "status_url": "https://queue.fal.run/s", "response_url": "https://queue.fal.run/r"},
        "GET": {"video": {"url": "https://v3b.fal.media/clip.mp4"}},
    }
    requests = []

    def request_json(method, url, **kwargs):
        requests.append((method, url))
        return replies[method]

    monkeypatch.setattr(fal_media, "request_json", request_json)
    monkeypatch.setattr(fal_media, "poll", lambda *args, **kwargs: None)
    monkeypatch.setattr(fal_media, "download", lambda url, path: str(path))
    monkeypatch.setattr("tools.analysis.audio_probe.probe_duration", lambda path: 5.4)
    return requests


@pytest.fixture()
def in_a_job(tmp_path, monkeypatch):
    directory = tmp_path / "agent"
    monkeypatch.setenv(job_ledger.LEDGER_DIR_ENV, str(directory))
    job_ledger.write_budget(directory, 1.0)
    return directory


def test_quotes_follow_the_route_and_the_requested_length():
    tool = H3MaxVideo()
    # The default 5 s plus the 0.7 s an output can run over.
    assert tool.estimate_cost({"prompt": "pour", "image_path": "still.png"}) == pytest.approx(0.035 * 5.7)
    assert tool.estimate_cost({"prompt": "pour", "image_url": "https://x/a.png", "duration": 2}) == pytest.approx(0.035 * 2.7)
    assert tool.estimate_cost({"prompt": "pour", "operation": "reference_to_video",
                               "provider_params": {"duration": 8}}) == pytest.approx(0.05 * 8.7)
    catalog = tool.get_info()["model_catalog"]["h3-max"]
    assert catalog["pricing_status"] == "priced"
    assert catalog["usd_per_output_second"]["image_to_video"] == 0.035


def test_a_route_without_a_rate_still_needs_a_quote():
    with pytest.raises(PriceQuoteRequired):
        WanFalVideo().estimate_cost({"prompt": "Clouds"})
    assert WanFalVideo().get_info()["model_catalog"]["wan-3.0"]["pricing_status"] == "quote_required"


def test_a_finished_clip_is_billed_for_its_real_length(fake_fal, tmp_path):
    result = H3MaxVideo().execute({"prompt": "pour", "image_url": "https://x/a.png",
                                   "output_path": str(tmp_path / "hook.mp4")})
    assert result.success, result.error
    assert result.cost_usd == pytest.approx(0.035 * 5.4)
    assert result.data["cost_status"] == "priced"


def test_in_a_job_the_clip_is_quoted_then_billed(fake_fal, in_a_job, tmp_path):
    assert H3MaxVideo().execute({"prompt": "pour", "image_url": "https://x/a.png",
                                 "output_path": str(tmp_path / "hook.mp4")}).success
    entries = job_ledger.read_entries(in_a_job)
    assert [e["event"] for e in entries] == ["start", "finish"]
    assert entries[0]["estimate_usd"] == pytest.approx(0.1995)
    assert job_ledger.summarize(entries).spent_usd == pytest.approx(0.189)


def test_in_a_job_an_unpriced_fal_route_is_refused_before_any_request(fake_fal, in_a_job):
    result = WanFalVideo().execute({"prompt": "Clouds"})
    assert result.success is False and "NO PRICE FOR THIS CALL" in result.error
    assert fake_fal == [], "fal never receives the job"
