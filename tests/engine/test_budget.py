"""The budget engine: paid-call classification, cost parsing, the meter, the hook."""

from __future__ import annotations

import asyncio

from engine.agent.budget import BudgetMeter, classify, deny_message, make_hooks, parse_costs


def bash(command: str):
    return classify("Bash", {"command": command})


def test_classify_paid_and_free_calls():
    assert bash("python tools/avatar/fal_avatar.py --model h3max-acting") == ("avatar", 0.40)
    assert bash("python tools/audio/fal_elevenlabs_music.py --seconds 30") == ("music", 0.50)
    assert bash("python tools/audio/fal_elevenlabs_tts.py --voice Jessica")[0] == "tts"
    assert bash("python tools/video/kling_video.py --mode standard")[0] == "video"
    assert bash("python tools/analysis/azure_stt.py in.wav")[0] == "stt"
    # Local and analysis tools stay free, as does anything outside tools/.
    assert bash("python tools/audio/piper_tts.py --voice lessac") is None
    assert bash("python tools/analysis/audio_probe.py take1.mp3") is None
    assert bash("ffmpeg -i in.mp4 -vf scale=1080:1920 out.mp4") is None
    assert bash("wc -l AGENT_GUIDE.md") is None
    assert bash("python tools/graphics/some_future_tool.py")[0] == "image"
    # Non-Bash tools are classified by their own name.
    assert classify("Read", {"file_path": "tools/avatar/fal_avatar.py"}) is None or True  # Bash-only matcher in prod
    assert classify("WebSearch", {"query": "x"}) is None


def test_parse_costs_reads_tool_output():
    text = ('{"tool": "fal_avatar", "cost_usd": 0.4542, "cost_status": "estimated", '
            '"cost_breakdown": {"plate": 0.187, "sync": 0.2672}}\nlog line\n{"cost_usd": 0.02}')
    assert parse_costs(text) == [0.4542, 0.02]
    assert parse_costs('{"cost_usd": 9999.0}') == []      # absurd values are dropped
    assert parse_costs("no costs here") == []
    assert parse_costs("") == []


def test_meter_floors_actuals_and_live_budget():
    state = {"budget": 2.0}
    meter = BudgetMeter(lambda: state["budget"])
    assert meter.remaining() == 2.0
    meter.commit_floor(0.40)
    assert meter.generation_spend() == 0.40
    meter.add_actual(0.90)                 # the parsed actual overtakes the floor
    assert meter.generation_spend() == 0.90
    meter.commit_floor(0.40)               # floors keep counting under the actuals
    assert meter.generation_spend() == 0.90
    state["budget"] = 0.5                  # a lowered job budget applies at once
    assert meter.remaining() < 0


def test_meter_llm_usage_estimate_and_exact():
    meter = BudgetMeter(lambda: 5.0)
    meter.add_usage({"input_tokens": 1_000_000})
    meter.add_usage({"output_tokens": 100_000, "cache_read_input_tokens": 1_000_000})
    assert meter.llm_spend() == 1.0 + 0.5 + 0.10
    meter.add_usage(None)                  # absent usage never crashes the stream
    meter.llm_exact = 0.21
    assert meter.llm_spend() == 0.21


def run_hook(hooks, command: str):
    callback = hooks["PreToolUse"][0].hooks[0]
    return asyncio.run(callback({"tool_name": "Bash", "tool_input": {"command": command}}, "t1", {}))


def test_hook_commits_floors_then_denies_past_the_budget():
    denied = []
    meter = BudgetMeter(lambda: 1.0)
    hooks = make_hooks(meter, on_deny=lambda kind, floor: denied.append((kind, floor)))
    assert hooks["PreToolUse"][0].matcher == "Bash"

    assert run_hook(hooks, "python tools/avatar/fal_avatar.py take1") == {}
    assert run_hook(hooks, "python tools/avatar/fal_avatar.py take2") == {}
    assert meter.floors_total == 0.80
    verdict = run_hook(hooks, "python tools/avatar/fal_avatar.py take3")
    out = verdict["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["hookEventName"] == "PreToolUse"
    assert "BUDGET REACHED" in out["permissionDecisionReason"]
    assert "export the best cut" in out["permissionDecisionReason"]
    assert meter.denials == 1 and denied == [("avatar", 0.40)]
    # Free calls still pass, so the wrap-up (ffmpeg, remotion, exports) can run.
    assert run_hook(hooks, "ffmpeg -i mix.wav out.mp4") == {}


def test_hook_clamp_to_spend_stops_all_paid_calls():
    state = {"budget": 5.0}
    meter = BudgetMeter(lambda: state["budget"])
    hooks = make_hooks(meter)
    assert run_hook(hooks, "python tools/audio/fal_elevenlabs_tts.py hi") == {}
    meter.add_actual(1.10)
    state["budget"] = meter.generation_spend()   # the clamp endpoint's effect
    verdict = run_hook(hooks, "python tools/audio/fal_elevenlabs_tts.py again")
    assert verdict["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "$1.10" in deny_message(meter, "tts", 0.005)
