"""The budget: the tool-layer guard and job ledger, tested the way the agent reaches paid tools.

The agent calls tools through the registry in throwaway ``python -c`` processes, so enforcement
lives in BaseTool.execute and these tests go through it (one of them through the real fal_avatar in
a separate process), rather than matching command strings.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from engine.agent.budget import BudgetMeter
from lib import job_ledger
from tools.base_tool import BaseTool, ToolResult

REPO_ROOT = Path(__file__).resolve().parents[2]


class PaidTool(BaseTool):
    name = "fake_paid"
    provider = "fal.ai"
    runs = 0

    def estimate_cost(self, inputs):
        if inputs.get("no_quote"):
            raise ValueError("a current price quote is required")
        return inputs.get("estimate", 0.50)

    def execute(self, inputs):
        type(self).runs += 1
        if inputs.get("boom"):
            raise RuntimeError("provider blew up")
        return ToolResult(success=inputs.get("success", True), cost_usd=inputs.get("cost", 0.40))


class FreeTool(BaseTool):
    name = "fake_free"

    def execute(self, inputs):
        return ToolResult(success=True, cost_usd=0.0)


class SelectorTool(BaseTool):
    name = "fake_selector"
    provider = "selector"

    def estimate_cost(self, inputs):
        return 0.50

    def execute(self, inputs):
        return PaidTool().execute(inputs)


@pytest.fixture()
def ledger_dir(tmp_path, monkeypatch):
    directory = tmp_path / "agent"
    monkeypatch.setenv(job_ledger.LEDGER_DIR_ENV, str(directory))
    job_ledger.write_budget(directory, 1.0)
    PaidTool.runs = 0
    return directory


def state_of(directory):
    return job_ledger.summarize(job_ledger.read_entries(directory))


def test_paid_calls_are_recorded_then_denied_past_the_budget(ledger_dir):
    assert PaidTool().execute({"model": "h3max", "output_path": "/p/take1.mp4"}).success
    assert PaidTool().execute({"output_path": "/p/take2.mp4"}).success
    assert state_of(ledger_dir).spent_usd == 0.80

    denied = PaidTool().execute({"output_path": "/p/take3.mp4"})     # 0.80 + 0.50 is past 1.00
    assert denied.success is False
    assert "BUDGET REACHED" in denied.error and "export the best cut" in denied.error
    assert "$0.80" in denied.error and "fake_paid" in denied.error
    assert PaidTool.runs == 2, "a denied call never reaches the provider"

    state = state_of(ledger_dir)
    assert state.spent_usd == 0.80 and [d["tool"] for d in state.denials] == ["fake_paid"]
    assert [(i.item, i.usd, i.provider) for i in state.items] == [
        ("fake_paid h3max (take1.mp4)", 0.40, "fal.ai"),
        ("fake_paid (take2.mp4)", 0.40, "fal.ai"),
    ]
    # Free calls still pass, so the wrap-up (mixing, rendering, exporting) can run.
    assert FreeTool().execute({}).success


def test_lowering_the_budget_stops_the_next_paid_call(ledger_dir):
    assert PaidTool().execute({}).success
    job_ledger.write_budget(ledger_dir, state_of(ledger_dir).spent_usd)     # the clamp's effect
    assert PaidTool().execute({"estimate": 0.01}).success is False
    job_ledger.write_budget(ledger_dir, 5.0)                                # a top-up raises it again
    assert PaidTool().execute({}).success


def test_a_call_with_no_quote_passes_until_the_budget_is_spent(ledger_dir):
    assert PaidTool().execute({"no_quote": True, "cost": 1.0}).success
    denied = PaidTool().execute({"no_quote": True})
    assert denied.success is False and "cannot be quoted" in denied.error


def test_a_resume_is_never_denied(ledger_dir):
    job_ledger.write_budget(ledger_dir, 0.01)
    assert PaidTool().execute({"resume_job": {"status_url": "x"}, "cost": 0.7}).success
    assert state_of(ledger_dir).spent_usd == 0.70, "what the resumed job cost is still recorded"


def test_a_call_that_never_finished_counts_at_its_estimate(ledger_dir):
    call = job_ledger.begin_call(ledger_dir, tool="fake_paid", provider="fal.ai",
                                 inputs={"output_path": "/p/take1.mp4"}, estimate_usd=0.80)
    state = state_of(ledger_dir)
    assert state.spent_usd == 0.80, "running or killed, it may be billed"
    assert state.items[0].item == "fake_paid (take1.mp4) (not finished, estimated)"
    assert PaidTool().execute({"estimate": 0.30}).success is False, "the reservation counts against the budget"

    job_ledger.finish_call(ledger_dir, call, cost_usd=0.72, success=True)
    assert state_of(ledger_dir).spent_usd == 0.72, "the real cost replaces the estimate"


def test_a_later_success_for_the_same_output_replaces_an_unfinished_call(ledger_dir):
    job_ledger.begin_call(ledger_dir, tool="fake_paid", provider="fal.ai",
                          inputs={"output_path": "/p/take1.mp4"}, estimate_usd=0.50)   # killed at the limit
    assert PaidTool().execute({"output_path": "/p/take1.mp4", "resume_job": {"id": "1"}, "cost": 0.40}).success
    assert state_of(ledger_dir).spent_usd == 0.40


def test_a_selector_and_its_provider_are_one_paid_call(ledger_dir):
    assert SelectorTool().execute({"estimate": 0.50}).success
    state = state_of(ledger_dir)
    assert [i.item for i in state.items] == ["fake_selector"] and state.spent_usd == 0.40


def test_every_tool_that_ran_is_noted_in_order(ledger_dir):
    assert FreeTool().execute({}).success
    assert PaidTool().execute({}).success
    assert SelectorTool().execute({"estimate": 0.10}).success
    assert state_of(ledger_dir).tools_started == ["fake_free", "fake_paid", "fake_selector"]


def test_a_failed_call_costs_what_it_reported(ledger_dir):
    assert PaidTool().execute({"success": False, "cost": 0.0}).success is False
    assert state_of(ledger_dir).spent_usd == 0.0
    assert PaidTool().execute({"success": False, "cost": 0.10}).success is False
    assert state_of(ledger_dir).spent_usd == 0.10


def test_a_crashing_tool_raises_and_leaves_nothing_open(ledger_dir):
    with pytest.raises(RuntimeError):
        PaidTool().execute({"boom": True})
    assert state_of(ledger_dir).spent_usd == 0.0


def test_outside_a_job_nothing_is_checked_or_written(tmp_path, monkeypatch):
    monkeypatch.delenv(job_ledger.LEDGER_DIR_ENV, raising=False)
    PaidTool.runs = 0
    assert PaidTool().execute({"estimate": 999.0}).success and PaidTool.runs == 1
    assert list(tmp_path.iterdir()) == []


def test_a_broken_ledger_folder_never_breaks_a_tool(tmp_path, monkeypatch):
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(job_ledger.LEDGER_DIR_ENV, str(blocker))
    assert PaidTool().execute({}).success


def test_malformed_ledger_lines_are_skipped(ledger_dir):
    assert PaidTool().execute({}).success
    with open(ledger_dir / job_ledger.LEDGER_FILENAME, "a", encoding="utf-8") as fh:
        fh.write('{"event": "finish", "cost_us\n[1, 2]\n')
    assert state_of(ledger_dir).spent_usd == 0.40


def agent_python(code: str, directory: Path) -> str:
    """Run code the way the agent's Bash calls do: a fresh process that only has the session's env."""
    env = {**os.environ, job_ledger.LEDGER_DIR_ENV: str(directory), "PYTHONPATH": str(REPO_ROOT)}
    done = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True,
                          text=True, timeout=240, check=True)
    return done.stdout


def test_spend_carries_across_the_agents_processes(tmp_path):
    directory = tmp_path / "agent"
    job_ledger.write_budget(directory, 1.0)
    code = (
        "from tools.base_tool import BaseTool, ToolResult\n"
        "class Paid(BaseTool):\n"
        "    name = 'paid'\n"
        "    def estimate_cost(self, inputs): return 0.60\n"
        "    def execute(self, inputs): return ToolResult(success=True, cost_usd=0.60)\n"
        "r = Paid().execute({})\n"
        "print(r.success)\n"
    )
    assert agent_python(code, directory).strip() == "True"
    assert agent_python(code, directory).strip() == "False", "the second process sees the first one's spend"
    assert state_of(directory).spent_usd == 0.60


def test_the_real_avatar_tool_called_the_way_the_agent_calls_it_is_denied(tmp_path):
    """The shape of a real paid call (job_b2d53fa207a664ae), which a check on command strings missed.
    The budget is below the quote, so it is denied before any provider is contacted."""
    directory = tmp_path / "agent"
    job_ledger.write_budget(directory, 0.50)
    output = str(tmp_path / "host_take_v1.mp4")
    code = (
        "from tools.tool_registry import registry\n"
        "registry.discover()\n"
        "t=registry._tools['fal_avatar']\n"
        f"inp={{'model':'h3max-acting','image_path':'portrait.png','audio_duration_seconds':10,'output_path':{output!r}}}\n"
        "r=t.execute(inp)\n"
        "print(r.success, r.cost_usd)\n"
        "print(r.error)\n"
    )
    stdout = agent_python(code, directory)
    assert stdout.startswith("False"), stdout
    assert "PAID-GENERATION BUDGET REACHED" in stdout and "fal_avatar" in stdout
    state = state_of(directory)
    assert state.spent_usd == 0.0
    assert state.denials[0]["tool"] == "fal_avatar" and state.denials[0]["estimate_usd"] == pytest.approx(0.85)
    assert not Path(output).exists()


def test_budget_file_is_written_whole(tmp_path):
    job_ledger.write_budget(tmp_path, 2.5)
    assert job_ledger.read_budget(tmp_path) == 2.5
    assert json.loads((tmp_path / job_ledger.BUDGET_FILENAME).read_text(encoding="utf-8")) == {"max_budget_usd": 2.5}
    assert [p.name for p in tmp_path.iterdir()] == [job_ledger.BUDGET_FILENAME]
    assert job_ledger.read_budget(tmp_path / "missing") is None


def test_meter_mirrors_spend_and_the_live_budget():
    state = {"budget": 2.0}
    meter = BudgetMeter(lambda: state["budget"])
    assert meter.remaining() == 2.0
    meter.actuals_total = 0.90
    assert meter.generation_spend() == 0.90
    state["budget"] = 0.5                  # a lowered job budget applies at once
    assert meter.remaining() < 0


def test_meter_llm_usage_estimate_and_exact():
    meter = BudgetMeter(lambda: 5.0)
    meter.add_usage({"input_tokens": 1_000_000})
    meter.add_usage({"output_tokens": 100_000, "cache_read_input_tokens": 1_000_000})
    assert meter.llm_spend() == pytest.approx(0.30 + 0.15 + 0.05)
    meter.add_usage(None)                  # absent usage never crashes the stream
    meter.llm_exact = 0.21
    assert meter.llm_spend() == 0.21


def test_meter_counts_one_api_call_once():
    """The stream repeats a call's usage on every content block it emits."""
    meter = BudgetMeter(lambda: 5.0)
    usage = {"cache_read_input_tokens": 1_000_000}
    for _ in range(3):
        meter.add_usage(usage, "msg_1")
    meter.add_usage(usage, "msg_2")
    assert meter.llm_spend() == 0.10


def test_meter_rates_track_the_exact_cost_of_real_jobs():
    """Token counts as the live stream reports them on real finished jobs, against the SDK's exact cost."""
    for read, write, exact in [(3_590_600, 134_175, 0.2297), (5_735_635, 153_273, 0.3494)]:
        meter = BudgetMeter(lambda: 5.0)
        meter.add_usage({"cache_read_input_tokens": read, "cache_creation_input_tokens": write}, "msg")
        assert meter.llm_spend() == pytest.approx(exact, rel=0.05)
