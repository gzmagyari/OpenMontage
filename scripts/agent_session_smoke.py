#!/usr/bin/env python3
"""Live check of the agent session setup. Run it after every image rebuild and runtime upgrade.

How a job behaves on a slow tool call, and whether the budget reaches the tools, is decided by the
Claude Agent SDK and the CLI it bundles, so unit tests with a fake stream can't prove it. This runs
one real session on the production options, about $0.01 of Haiku and no paid tools:

  1. a 130-second command, past the CLI's stock 120 s Bash limit;
  2. the real fal_avatar called the way the agent calls it, on a budget below its quote;
  3. one more Bash call, which is the call that used to be refused after a background move.

It passes when the job stayed one continuous turn, nothing moved to the background or was refused,
the budget denied the paid call, and only the allowed tools were offered.

    docker exec -w /app openmontage-api python scripts/agent_session_smoke.py
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from claude_agent_sdk import ResultMessage, SystemMessage  # noqa: E402

from engine.agent.session import AGENT_TOOLS, AgentSession  # noqa: E402
from lib import job_ledger  # noqa: E402

BUDGET_USD = 0.50  # below the avatar quote (10 s = $0.85); no portrait exists, so nothing can be bought anyway
AVATAR_CALL = (
    "python -c \"from tools.tool_registry import registry; registry.discover(); "
    "t=registry._tools['fal_avatar']; "
    "r=t.execute({'model':'h3max-acting','image_path':'no_such_portrait.png','audio_duration_seconds':10,"
    "'output_path':'/tmp/agent_session_smoke.mp4'}); print(r.success); print(r.error)\""
)
PROMPT = (
    "Plumbing test, ignore any onboarding. Run these one at a time with the Bash tool, exactly as written, "
    "with no timeout parameter, and do nothing else.\n"
    "1. python -c \"import time; time.sleep(130); print('finished')\"\n"
    f"2. {AVATAR_CALL}\n"
    "3. ls /tmp | head -2\n"
    "Then reply with the word DONE."
)


async def run() -> int:
    work = Path(tempfile.mkdtemp(prefix="agent_session_smoke_"))
    job_ledger.write_budget(work, BUDGET_USD)
    results: list[str] = []
    turns = 0
    tools: list[str] = []

    def on_event(entry: dict) -> None:
        if entry.get("kind") == "tool":
            print(f"  tool: {str(entry.get('input'))[:90]}", flush=True)
        elif entry.get("kind") == "tool_result":
            results.append(str(entry.get("text")))
            print(f"  result: {results[-1][:90]!r}", flush=True)

    def on_message(message) -> None:
        nonlocal turns, tools
        if isinstance(message, ResultMessage):
            turns += 1
        elif isinstance(message, SystemMessage) and message.subtype == "init":
            tools = list((message.data or {}).get("tools") or [])

    session = AgentSession(
        repo_root=REPO_ROOT,
        journal_path=work / "journal.jsonl",
        transcript_path=work / "transcript.jsonl",
        model="claude-haiku-5-5",
        max_turns=12,
        max_claude_usd=0.30,
        env_extra={job_ledger.LEDGER_DIR_ENV: str(work)},
        on_event=on_event,
        on_message=on_message,
    )
    try:
        outcome = await asyncio.wait_for(session.run(PROMPT), timeout=420)
        denials = job_ledger.summarize(job_ledger.read_entries(work)).denials
    finally:
        shutil.rmtree(work, ignore_errors=True)

    text = "\n".join(results)
    checks = {
        "the session succeeded": outcome.succeeded,
        "one continuous turn": turns == 1,
        "only the allowed tools were offered": sorted(tools) == sorted(AGENT_TOOLS),
        "nothing moved to the background": "moved to the background" not in text,
        "nothing was refused": "doesn't want to take this action" not in text,
        "the slow command finished in the foreground": "finished" in text,
        "the budget denied the paid call": "PAID-GENERATION BUDGET REACHED" in text
        and [d.get("tool") for d in denials] == ["fal_avatar"],
    }
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
    print(f"cost ${outcome.total_cost_usd:.4f}, {outcome.num_turns} model calls")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
