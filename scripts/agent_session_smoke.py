#!/usr/bin/env python3
"""Live check of the agent session setup. Run it after every image rebuild and runtime upgrade.

How a job behaves on a slow tool call, and whether the budget reaches the tools, is decided by the
Claude Agent SDK and the CLI it bundles, so unit tests with a fake stream can't prove it. This runs
one real session on the production options, about $0.01 of Haiku and no paid tools:

  1. a 130-second command, past the CLI's stock 120 s Bash limit;
  2. the real fal_avatar called the way the agent calls it, on a budget below its quote;
  3. one more Bash call, which is the call that used to be refused after a background move.

  4. WebFetch on a public page, through the box's web proxy.

The session runs in a job's box (engine/sandbox/), as every production job does. It passes when the
job stayed one continuous turn, nothing moved to the background or was refused, the budget denied
the paid call at the desk, WebFetch reached the page, and only the allowed tools were offered.
Run scripts/agent_box_check.py first: it checks the box itself, at no cost.

    docker exec -w /app openmontage-api python scripts/agent_session_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from claude_agent_sdk import ResultMessage, SystemMessage  # noqa: E402

from engine.agent.session import AGENT_TOOLS, AgentSession, anthropic_api_key, cli_switches  # noqa: E402
from engine.sandbox.box import AgentBox, sandbox_problem  # noqa: E402
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
    "4. Use the WebFetch tool on https://example.com and report the page title.\n"
    "Then reply with the word DONE."
)


async def run() -> int:
    problem = sandbox_problem()
    if problem:
        print(f"FAIL  this host cannot build agent boxes: {problem}")
        return 1
    # Under the data folder, like a job: a box's /tmp is its own, so the project can't live in /tmp.
    work = Path(tempfile.mkdtemp(prefix="agent_session_smoke_", dir=os.environ.get("OPENMONTAGE_DATA_DIR") or "/data"))
    project = work / "projects" / "job_smoke"
    project.mkdir(parents=True)
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

    os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    try:
        async with AgentBox(agent_dir=work, project_dir=project, model="claude-haiku-5-5", claude_cap_usd=0.45,
                            cli_switches=cli_switches(), api_key=anthropic_api_key(REPO_ROOT)) as box:
            session = AgentSession(
                repo_root=REPO_ROOT,
                journal_path=work / "journal.jsonl",
                transcript_path=work / "transcript.jsonl",
                model="claude-haiku-5-5",
                max_turns=14,
                max_claude_usd=0.30,
                on_event=on_event,
                on_message=on_message,
                cli_path=box.cli_path,
                box_env=box.session_env(),
                settings_json=box.settings_json,
            )
            outcome = await asyncio.wait_for(session.run(PROMPT), timeout=480)
        denials = job_ledger.summarize(job_ledger.read_entries(work)).denials
        fetched = (work / "web.log").read_text(encoding="utf-8") if (work / "web.log").is_file() else ""
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
        "the budget denied the paid call at the desk": "PAID-GENERATION BUDGET REACHED" in text
        and [d.get("tool") for d in denials] == ["fal_avatar"],
        "WebFetch reached the page through the web proxy": "example.com" in fetched and "Example Domain" in text,
    }
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
    print(f"cost ${outcome.total_cost_usd:.4f}, {outcome.num_turns} model calls")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
