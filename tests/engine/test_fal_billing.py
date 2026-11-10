"""Billing truth: real rates, request-id plumbing, and settlement reconciliation."""

from __future__ import annotations

from pathlib import Path

from lib import fal_billing, job_ledger
from lib.job_ledger import CostItem


# -- the measured rates (fal billing events, 2026-11-09) --------------------------------

def test_the_avatar_composite_prices_at_the_real_billed_rates():
    from tools.avatar.fal_avatar import FalAvatar

    tool = FalAvatar()
    # 5.1s audio bills as 6 whole seconds: plate $0.056/s + lip-sync $0.20/s at 768P.
    assert tool.estimate_cost({"audio_duration_seconds": 5.1, "model": "h3max-acting",
                               "image_url": "http://x/p.png", "audio_url": "http://x/a.mp3"}) == round(6 * 0.256, 4)
    # 480P keeps the pricing API's base plate rate.
    assert tool.estimate_cost({"audio_duration_seconds": 5.1, "resolution": "480P", "model": "h3max-acting",
                               "image_url": "http://x/p.png", "audio_url": "http://x/a.mp3"}) == round(6 * 0.235, 4)
    # Lip-sync fix mode is the sync pass alone: $0.20/s.
    assert tool.estimate_cost({"audio_duration_seconds": 9.2,
                               "video_url": "http://x/v.mp4", "audio_url": "http://x/a.mp3"}) == round(10 * 0.20, 4)


def test_native_takes_price_at_the_plate_rate_alone():
    from tools.avatar.fal_avatar import FalAvatar

    tool = FalAvatar()
    base = {"image_url": "http://x/p.png", "audio_url": "http://x/a.mp3", "audio_duration_seconds": 5.1}
    # h3max (native) = the plate alone; it is also the DEFAULT when no model is named.
    assert tool.estimate_cost({**base, "model": "h3max"}) == round(6 * 0.056, 4)
    assert tool.estimate_cost(base) == round(6 * 0.056, 4)
    assert tool.estimate_cost({**base, "model": "h3max", "resolution": "480P"}) == round(6 * 0.035, 4)
    # The composite stays the premium option, 4.6x the native price.
    assert tool.estimate_cost({**base, "model": "h3max-acting"}) == round(6 * 0.256, 4)
    enum = tool.input_schema["properties"]["model"]
    assert enum["default"] == "h3max" and "h3max" in enum["enum"] and "h3max-acting" in enum["enum"]


def test_a_take_is_priced_by_the_plate_length_fal_is_asked_for():
    """fal bills a plate for the whole seconds it is ASKED for, not for the line and not for the
    clip that comes back (billing events: video 714's 5.54 s line, asked 7, billed $0.392; the
    test take of 2026-11-10, a 4.13 s line asked as 4 with a 4.46 s clip back, billed $0.224).
    What is asked for is the fewest whole seconds whose clip holds the line, so the quote is
    that many seconds."""
    from tools.avatar.fal_avatar import FalAvatar

    tool = FalAvatar()
    base = {"image_url": "http://x/p.png", "audio_url": "http://x/a.mp3"}
    for line, seconds, billed in [(5.542, 6, 0.336), (4.133, 4, 0.224), (5.042, 5, 0.28), (7.24, 8, 0.448)]:
        assert FalAvatar._plate_seconds(line) == seconds
        assert tool.estimate_cost({**base, "audio_duration_seconds": line}) == billed
    # The plate's limits: never under 2 seconds, never over 15.
    assert FalAvatar._plate_seconds(0.4) == 2 and FalAvatar._plate_seconds(14.5) == 15
    # The composite adds the lip-sync pass, which follows the line: 6 s of plate, 6 s of sync.
    assert tool.estimate_cost({**base, "audio_duration_seconds": 5.542, "model": "h3max-acting"}) \
        == round(6 * 0.056 + 6 * 0.20, 4)
    # A lip fix on an existing take has no plate.
    assert tool.estimate_cost({"video_url": "http://x/v.mp4", "audio_url": "http://x/a.mp3",
                               "audio_duration_seconds": 5.542}) == round(6 * 0.20, 4)


def test_h3_video_applies_the_768p_multiplier_and_bills_what_was_asked_for():
    from tools.video.h3_max_video import H3MaxVideo

    tool = H3MaxVideo()
    ep = "minimax/h3-max/image-to-video"
    assert tool._billed_rate(ep, {"resolution": "768P"}) == round(0.035 * 1.6, 4)
    assert tool._billed_rate(ep, {"resolution": "480P"}) == 0.035
    assert tool._billed_rate(ep, {}) == round(0.035 * 1.6, 4)  # hi-res is the default
    # fal bills the length asked for: the four 5-second clips of videos 713 and 714 were billed
    # $0.28 each, though every file runs 5.18 s. The estimate and the recorded cost said $0.336.
    clip = {"image_url": "http://x/p.png", "prompt": "the desk, slow push in", "duration": 5}
    assert tool.estimate_cost(clip) == 0.28
    assert tool._output_cost(ep, "/nonexistent.mp4", clip) == 0.28
    assert tool.estimate_cost({**clip, "duration": 8}) == round(8 * 0.056, 4)
    assert tool.estimate_cost({**clip, "duration": None, "provider_params": {"duration": 10}}) == 0.56
    assert tool.estimate_cost({**clip, "resolution": "480P"}) == round(5 * 0.035, 4)
    # A length that is not whole still rounds up, never down.
    assert tool._output_cost(ep, "/nonexistent.mp4", {"duration": 5.18}) == round(6 * 0.056, 4)


# -- request ids through the ledger ------------------------------------------------------

def test_finish_call_records_request_ids_and_summarize_returns_them(tmp_path: Path):
    call = job_ledger.begin_call(tmp_path, tool="fal_avatar", provider="fal.ai",
                                 inputs={"output_path": str(tmp_path / "take.mp4")},
                                 estimate_usd=1.0)
    job_ledger.finish_call(tmp_path, call, cost_usd=1.536, success=True,
                           request_ids=["rid-plate", "rid-sync", None])
    state = job_ledger.summarize(job_ledger.read_entries(tmp_path))
    assert len(state.items) == 1
    assert state.items[0].request_ids == ("rid-plate", "rid-sync")
    assert state.items[0].estimated is True


# -- reconciliation ----------------------------------------------------------------------

def test_reconcile_replaces_fully_matched_items_with_actuals():
    items = [
        CostItem("take_A", 0.51, "fal.ai", ("p1", "s1")),   # the old 2.4x-under estimate
        CostItem("broll", 0.1814, "fal.ai", ("b1",)),
        CostItem("music", 0.60, "fal.ai", ()),              # no ids: stays an estimate
        CostItem("take_B", 1.536, "fal.ai", ("p2", "s2")),  # s2 unbilled: stays an estimate
    ]
    events = {"p1": 0.336, "s1": 1.20, "b1": 0.336, "p2": 0.336}
    out, stats = fal_billing.reconcile(items, events)
    assert [i.usd for i in out] == [1.536, 0.336, 0.60, 1.536]
    assert [i.estimated for i in out] == [False, False, True, True]
    assert stats["matched"] == 2 and stats["unmatched"] == 1 and stats["no_ids"] == 1
    # Drift is actual minus estimate over the matched items only.
    assert stats["estimated_usd"] == round(0.51 + 0.1814, 4)
    assert stats["actual_usd"] == round(1.536 + 0.336, 4)
    assert stats["drift_usd"] == round((1.536 - 0.51) + (0.336 - 0.1814), 4)


def test_fal_charges_without_a_request_id_are_named_not_passed_over():
    items = [
        CostItem("take_A", 0.28, "fal.ai", ("p1",)),
        CostItem("music", 0.60, "fal.ai", ()),            # a fal charge nobody can check
        CostItem("portrait", 0.0413, "openai", ()),       # not fal's to bill
        CostItem("mix", 0.0, "fal.ai", ()),               # nothing was charged
    ]
    assert [i.item for i in fal_billing.unchecked(items)] == ["music"]
    assert fal_billing.unchecked([]) == []


def test_the_voice_and_music_tools_hand_their_request_ids_to_the_ledger():
    """Both returned their request id only inside the resume record, so their lines could never
    be checked against fal's billing."""
    import inspect

    from tools.audio.fal_elevenlabs_music import FalElevenLabsMusic
    from tools.audio.fal_elevenlabs_tts import FalElevenLabsTTS
    from tools.audio.fal_lyria_music import FalLyriaMusic

    for tool in (FalElevenLabsMusic, FalElevenLabsTTS, FalLyriaMusic):
        assert '"request_ids"' in inspect.getsource(tool), tool.__name__


def test_reconciled_items_flow_into_cost_lines_with_estimated_false():
    from engine.agent.runner import _set_generation_lines

    class _Cost:
        def __init__(self):
            self.lines = []
            self.spent_usd = 0.0

    class _Job:
        def __init__(self):
            self.cost = _Cost()

    class _Store:
        def save(self, job):
            pass

    class _Ctx:
        def __init__(self):
            self.job = _Job()
            self.store = _Store()

    ctx = _Ctx()
    _set_generation_lines(ctx, [CostItem("take_A", 1.536, "fal.ai", ("p1",), estimated=False),
                                CostItem("music", 0.60, "fal.ai", ())])
    flags = {line.item: line.estimated for line in ctx.job.cost.lines}
    assert flags == {"take_A": False, "music": True}
    assert ctx.job.cost.spent_usd == round(1.536 + 0.60, 4)
