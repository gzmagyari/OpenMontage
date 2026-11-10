"""The authoring rehearsal (scripts/kit_authoring_probe.py): the parts that need no model.

What a rehearsal can and cannot buy, the prompt it sends, and how it reads what the director
left behind. The session itself is the production one and is exercised by the script.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from engine.agent.brief import build_brief
from engine.sandbox import box
from lib import job_ledger
from lib.videokit import REPO_ROOT, stage

_SPEC = importlib.util.spec_from_file_location("kit_authoring_probe", REPO_ROOT / "scripts" / "kit_authoring_probe.py")
assert _SPEC is not None and _SPEC.loader is not None
probe = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = probe
_SPEC.loader.exec_module(probe)


# ---------------------------------------------------------------- nothing can be bought

def test_provider_credentials_are_replaced_and_the_model_key_is_kept():
    env = {"FAL_KEY": "real-fal", "OPENAI_API_KEY": "real-openai", "ELEVENLABS_API_KEY": "real-eleven",
           "REPLICATE_API_TOKEN": "real-token", "HIGGSFIELD_API_SECRET": "real-secret", "VOLC_ACCESSKEY": "real-ak",
           "OPENMONTAGE_FAL_ADMIN_KEY": "real-admin", "ANTHROPIC_API_KEY": "the-model-key",
           "OPENMONTAGE_AGENT_LLM_UPSTREAM_KEY": "the-upstream-key", "SUNO_API_KEY": "",
           "OPENMONTAGE_AGENT_MODEL": "claude-sonnet-5-5", "PATH": "/usr/bin"}
    replaced = probe.neutralize_provider_keys(env)
    assert replaced == ["ELEVENLABS_API_KEY", "FAL_KEY", "HIGGSFIELD_API_SECRET", "OPENAI_API_KEY",
                        "OPENMONTAGE_FAL_ADMIN_KEY", "REPLICATE_API_TOKEN", "VOLC_ACCESSKEY"]
    assert all(env[name] == probe.PLACEHOLDER for name in replaced)
    # The session still reaches its model, plain settings are untouched, and a provider that
    # was not configured stays not configured (the menu the director reads does not change).
    assert env["ANTHROPIC_API_KEY"] == "the-model-key" and env["OPENMONTAGE_AGENT_LLM_UPSTREAM_KEY"] == "the-upstream-key"
    assert env["OPENMONTAGE_AGENT_MODEL"] == "claude-sonnet-5-5" and env["PATH"] == "/usr/bin" and env["SUNO_API_KEY"] == ""
    assert probe.neutralize_provider_keys(env) == []  # nothing real is left


def test_the_rehearsal_and_the_box_agree_on_what_a_credential_is():
    assert probe._SECRET_NAME.pattern == box._SECRET_NAME.pattern
    assert probe._SECRET_NAME.flags == box._SECRET_NAME.flags
    # The one credential the box keeps on the server and the rehearsal keeps for the session.
    assert "ANTHROPIC_API_KEY" in probe.LLM_CREDENTIALS and "ANTHROPIC_API_KEY" in box._SERVER_ONLY


def test_a_budget_of_zero_refuses_every_paid_call_and_lets_free_ones_through(tmp_path: Path):
    job_ledger.write_budget(tmp_path, 0.0)
    quoted = job_ledger.begin_call(tmp_path, tool="fal_avatar", provider="fal", inputs={}, estimate_usd=0.56)
    unquoted = job_ledger.begin_call(tmp_path, tool="mystery", provider="x", inputs={}, estimate_usd=None)
    free = job_ledger.begin_call(tmp_path, tool="transcriber", provider="local", inputs={}, estimate_usd=0.0)
    assert quoted.denied and unquoted.denied and not free.denied
    state = job_ledger.summarize(job_ledger.read_entries(tmp_path))
    assert [d.get("tool") for d in state.denials] == ["fal_avatar", "mystery"] and state.spent_usd == 0


# ---------------------------------------------------------------- the prompt

def test_the_rehearsal_is_the_real_brief_plus_where_to_stop(tmp_path: Path):
    brief = build_brief(prompt="show a dog running", references=[], project_dir=tmp_path / "job_x", budget_usd=6.0)
    prompt = probe.rehearsal_prompt(brief)
    assert prompt.startswith(brief.rstrip())  # nothing of the production brief is changed
    tail = " ".join(prompt[len(brief.rstrip()):].split())
    for phrase in ("exactly the video you would make with the budget above", "every paid tool call is refused",
                   "everything you would do before the first paid call", "while that batch is generating",
                   "placeholders", "no final render", "REHEARSAL DONE"):
        assert phrase in tail, phrase
    # It says where the run stops and never how to work: the brief, the guide and the skills
    # must carry that alone, or the rehearsal would test this paragraph instead of them.
    for hint in ("edit", "kit", "sheet", "scene", "look", "check --draft"):
        assert hint not in tail.lower(), hint


def test_prompts_come_from_a_file_or_from_two_that_name_no_product(tmp_path: Path):
    assert [p.key for p in probe.load_probes(None)] == ["story", "announcement"]
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps([{"key": "promo", "prompt": "A promo", "why": "the usual"},
                                {"key": "short", "prompt": "Text only", "seconds": 15, "music": False}]), encoding="utf-8")
    loaded = probe.load_probes(path)
    assert [(p.key, p.seconds, p.music, p.budget_usd) for p in loaded] == [("promo", 30.0, True, 6.0), ("short", 15, False, 6.0)]
    for bad in ([], [{"key": "a"}], [{"key": "Bad Key", "prompt": "x"}], [{"key": "a", "prompt": "x", "typo": 1}],
                [{"key": "a", "prompt": "x"}, {"key": "a", "prompt": "y"}], [{"key": "a", "prompt": "  "}]):
        path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(ValueError):
            probe.load_probes(path)


# ---------------------------------------------------------------- what the director left behind

def _staged(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    project = tmp_path / "job_x"
    project.mkdir(parents=True)
    stage.stage_workspace(project)
    return project, probe.fingerprint(project)


def test_an_untouched_workspace_reads_as_untouched(tmp_path: Path):
    project, staged = _staged(tmp_path)
    facts = probe.workspace_facts(project, staged)
    assert facts["edit"] == {"exists": True, "written": False, "valid_json": True, "lines": facts["edit"]["lines"],
                             "shots": 0, "overlays": 0, "voice_lines": 0, "scenes_used": []}
    assert facts["look"]["untouched_starter"] == stage.DEFAULT_LOOK and facts["look"]["token_lines_changed"] == 0
    assert facts["custom_scenes"] == [] and facts["unexpected_files"] == [] and facts["engine_markers"] == []
    assert facts["staged_files_changed"] == []
    # A skeleton nobody wrote is not an edit, even though it holds no engine code.
    verdict = list(probe.gate({"workspace": facts, "session": {}, "check": {"ok": True}, "sheets": []}).values())
    assert verdict[:probe.GATE_KEYS] == [False, True, False] and verdict[3] is False


def test_a_good_job_passes_and_engine_work_is_named(tmp_path: Path):
    project, staged = _staged(tmp_path)
    comp = project / "composition"
    (comp / "edit.json").write_text(json.dumps({
        "canvas": {"size": "1080x1920", "fps": 30}, "voice": {"lines": [{"id": "a", "text": "Hello there."}]},
        "main": [{"scene": "KineticText", "props": {"text": "Hi"}, "for": 2}, {"scene": "Hook", "for": 1}],
        "overlays": [{"tag": "new", "from": 0, "for": 1}]}, indent=1), encoding="utf-8")
    tokens = comp / "src" / "look" / "tokens.ts"
    tokens.write_text(tokens.read_text(encoding="utf-8").replace("#FFD400", "#35E0A1"), encoding="utf-8")
    (comp / "src" / "scenes").mkdir(exist_ok=True)
    (comp / "src" / "scenes" / "Hook.tsx").write_text(
        "import React from 'react';\nimport {Img, spring, useCurrentFrame} from 'remotion';\n\n"
        "export const Hook: React.FC = () => <div />;\n", encoding="utf-8")
    facts = probe.workspace_facts(project, staged)
    assert facts["edit"]["written"] and facts["edit"]["shots"] == 2 and facts["edit"]["scenes_used"] == ["Hook", "KineticText"]
    assert facts["look"]["untouched_starter"] is None and facts["look"]["closest_starter"] == stage.DEFAULT_LOOK
    assert facts["look"]["token_lines_changed"] == 2 and facts["look"]["files_changed"] == ["tokens.ts"]
    assert facts["custom_scenes"] == [{"file": "src/scenes/Hook.tsx", "lines": 3, "remotion": ["Img", "spring", "useCurrentFrame"]}]
    good = {"workspace": facts, "session": {"remotion_by_hand": [], "looked_at_sheet": True}, "check": {"ok": True},
            "sheets": ["work/kit/sheet.jpg"], "sheets_by_the_director": ["work/kit/sheet.jpg"],
            "spent_usd": 0.0, "paid_attempts": []}
    assert all(probe.gate(good).values())
    # A sheet the probe had to make itself still passes the gate, and is reported as such.
    probe_made = probe.gate({**good, "sheets_by_the_director": []})
    assert probe_made["a draft sheet renders"] and not probe_made["the director made a draft sheet and looked at it"]
    assert list(probe_made)[:probe.GATE_KEYS] == ["the director wrote the edit and it passes `kit check --draft`",
                                                  "no engine code was written", "a draft sheet renders"]

    # The engine's work turning up in the job: each kind is named, and the gate closes.
    (comp / "src" / "Root.tsx").write_text("import {Series} from 'remotion';\nexport const R = () => <Series />;\n", encoding="utf-8")
    (comp / "src" / "scenes" / "Sound.tsx").write_text("export const S = () => <Audio src='x' />;\n", encoding="utf-8")
    (comp / "src" / "index.ts").write_text("// rewritten\n", encoding="utf-8")
    facts = probe.workspace_facts(project, staged)
    assert facts["unexpected_files"] == ["src/Root.tsx"] and facts["staged_files_changed"] == ["src/index.ts"]
    # A hand-built caption file is engine work; a picture copied into composition/ is only untidy.
    (comp / "src" / "captions.json").write_text("[]", encoding="utf-8")
    (comp / "public").mkdir()
    (comp / "public" / "shot.jpg").write_bytes(b"jpg")
    facts = probe.workspace_facts(project, staged)
    assert facts["unexpected_files"] == ["src/Root.tsx", "src/captions.json"] and facts["stray_media"] == ["public/shot.jpg"]
    tidy = probe.workspace_facts(*_staged(tmp_path / "other"))
    assert tidy["stray_media"] == [] and tidy["unexpected_files"] == []
    assert facts["engine_markers"] == ["src/Root.tsx: sequences shots by hand", "src/scenes/Sound.tsx: plays the soundtrack by hand"]
    assert not probe.gate({**good, "workspace": facts})["no engine code was written"]
    # So does Remotion run by hand, and a paid attempt is reported beside the gate.
    by_hand = probe.gate({**good, "session": {"remotion_by_hand": ["npx remotion render"]}})
    assert not by_hand["no engine code was written"]
    assert not probe.gate({**good, "paid_attempts": ["fal_avatar"]})["nothing was bought and nothing paid was attempted"]


def test_the_starter_looks_hold_no_engine_marker(tmp_path: Path):
    # A marker must mean "the job did the engine's work", so nothing the kit ships may trip one.
    project, staged = _staged(tmp_path)
    for name in stage.looks():
        stage.set_look(project, name, force=True)
        assert probe.workspace_facts(project, staged)["engine_markers"] == [], name


def _journal(agent: Path, entries: list[dict]) -> None:
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "journal.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def test_the_session_is_read_from_the_journal_and_the_transcript(tmp_path: Path):
    agent, project = tmp_path / "agent", tmp_path / "projects" / "job_x"
    at = "2026-11-09T10:{:02d}:{:02d}+00:00".format
    calls = [
        ("t1", "Write", {"file_path": f"{project}/artifacts/video_plan.md", "content": "plan"}, at(1, 0), "ok", False),
        ("t2", "Write", {"file_path": f"{project}/composition/edit.json", "content": "{}"}, at(2, 0), "ok", False),
        ("t3", "Bash", {"command": f"cd {project} && kit check --draft"}, at(2, 30), "ERROR   word-not-found\nNOT OK: 1 error(s)", True),
        ("t4", "Edit", {"file_path": f"{project}/composition/edit.json", "old_string": "a", "new_string": "b"}, at(3, 0), "ok", False),
        ("t5", "Bash", {"command": f"cd {project} && kit check --draft 2>&1 | tail -5"}, at(3, 20), "OK: 29.5 s, 9 shots", False),
        ("t6", "Bash", {"command": f"cd {project} && kit sheet --draft && echo done"}, at(4, 0), "Sheet: work/kit/sheet.jpg (9 frames)", False),
        ("t7", "Bash", {"command": "cd composition && npx remotion still src/index.ts Video out.png"}, at(4, 30), "", False),
        ("t8", "Read", {"file_path": f"{project}/work/kit/sheet.jpg"}, at(4, 40), "[image]", False),
    ]
    journal: list[dict] = [{"at": at(0, 0), "kind": "session", "session_id": "s"}]
    transcript: list[dict] = []
    for call_id, name, tool_input, when, text, is_error in calls:
        journal.append({"at": when, "kind": "tool", "name": name, "id": call_id, "input": json.dumps(tool_input)[:60]})
        journal.append({"at": when[:-8] + f"{int(when[-8:-6]) + 5:02d}+00:00", "kind": "tool_result", "tool_use_id": call_id,
                        "is_error": is_error, "text": text})
        transcript.append({"type": "AssistantMessage", "content": [{"id": call_id, "name": name, "input": tool_input}]})
    # The journal clips a long result; the verdict of a kit run is read from the whole one.
    long_report = "WARNING shot-very-short [main[3]]: too brief\n" * 80 + "OK: 29.5 s, 9 shots"
    assert len(long_report) > 2000
    next(e for e in journal if e.get("tool_use_id") == "t5")["text"] = long_report[:2000]
    transcript.append({"type": "UserMessage", "content": [{"tool_use_id": "t5", "content": long_report, "is_error": False}]})
    transcript.append({"type": "UserMessage", "content": [{"tool_use_id": "t6", "is_error": False, "content": [
        {"type": "text", "text": "Sheet: work/kit/sheet.jpg (9 frames)"}]}]})
    transcript.append({"type": "TaskStartedMessage", "description": "write the hook scene", "data": {"subagent_type": "composer"}})
    transcript.append({"type": "ResultMessage", "result": "All set.\nREHEARSAL DONE", "usage": {
        "input_tokens": 40, "output_tokens": 9000, "output_tokens_details": {"thinking_tokens": 2500},
        "cache_creation_input_tokens": 70000, "cache_read_input_tokens": 900000, "server_tool_use": {"web_search_requests": 1}},
        "model_usage": {"claude-sonnet-5-5": {"costUSD": 0.61234}, "claude-haiku-5-5": {"costUSD": 0.01}}})
    _journal(agent, journal)
    (agent / "transcript.jsonl").write_text("".join(json.dumps(row) + "\n" for row in transcript), encoding="utf-8")

    facts = probe.session_facts(agent, project)
    assert facts["tools"] == {"Bash": 4, "Edit": 1, "Read": 1, "Write": 2}
    assert facts["writes"] == {"artifacts/video_plan.md": 1, "composition/edit.json": 2}
    assert [(run["ran"], run["ok"]) for run in facts["kit_runs"]] == [
        (["check --draft"], False), (["check --draft"], True), (["sheet --draft"], True)]
    assert facts["checks_before_the_first_pass"] == 1
    assert facts["edit_written_s"] == 120 and facts["check_passed_s"] == 205 and facts["sheet_made_s"] == 245
    assert len(facts["remotion_by_hand"]) == 1 and "remotion still" in facts["remotion_by_hand"][0]
    assert facts["looked_at_sheet"] is True
    assert facts["subagents"] == [{"type": "composer", "what": "write the hook scene"}]
    assert facts["tokens"] == {"input": 40, "output": 9000, "thinking": 2500, "cache_write": 70000,
                               "cache_read": 900000, "web_searches": 1}
    assert facts["cost_by_model"] == {"claude-sonnet-5-5": 0.6123, "claude-haiku-5-5": 0.01}
    assert facts["said_done"] and facts["journal_seconds"] == 285


def test_a_session_that_never_ran_reads_as_empty(tmp_path: Path):
    facts = probe.session_facts(tmp_path / "agent", tmp_path / "project")
    assert facts["tools"] == {} and facts["kit_runs"] == [] and facts["started"] is None and not facts["said_done"]
    assert facts["looked_at_sheet"] is False


def test_the_report_says_what_was_made_and_what_it_took():
    result = {
        "probe": "story", "why": "footage only", "prompt": "show a dog running", "passed": True, "stopped": None,
        "gate": {"the director wrote the edit and it passes `kit check --draft`": True, "no engine code was written": True,
                 "a draft sheet renders": True},
        "workspace": {"edit": {"lines": 41, "shots": 6, "overlays": 2, "voice_lines": 0, "scenes_used": ["KineticText"]},
                      "look": {"untouched_starter": None, "closest_starter": "pop", "token_lines_changed": 7, "files_changed": ["tokens.ts"]},
                      "custom_scenes": [{"file": "src/scenes/Paws.tsx", "lines": 28, "remotion": []}]},
        "check": {"errors": 0, "warnings": 1, "seconds": 29.8, "pending": ["a.mp4", "b.mp4"]},
        "session": {"kit_runs": [{"ran": ["check --draft"], "ok": False}, {"ran": ["check --draft", "sheet --draft"], "ok": True}],
                    "edit_written_s": 130, "check_passed_s": 200, "sheet_made_s": 215, "tokens": {"output": 8000}, "tools": {"Bash": 5}},
        "model_calls": 31, "wall_seconds": 252.0, "llm_usd": 0.71, "gateway_usd": 0.74, "spent_usd": 0.0,
    }
    text = probe.report([result], spend=True)
    for phrase in ("## story - PASSED", "> show a dog running", "- PASS  no engine code was written",
                   "edit: 41 lines, 6 shots, 2 overlays, 0 voice lines; scenes used: KineticText",
                   "check --draft: 0 error(s), 1 warning(s), 29.8 s; still to generate: 2 file(s)",
                   "look: restyled (from pop, 7 token lines changed, files changed: tokens.ts)",
                   "custom scenes: src/scenes/Paws.tsx (28 lines)", "took: 31 model calls, 4:12 wall, model cost $0.71",
                   "edit first written at 2:10, first passing check at 3:20, first sheet at 3:35",
                   "kit runs: check --draft (failed), check --draft+sheet --draft",
                   "Model cost in total: $0.71 (gateway meters: $0.74). Paid generation: $0.00."):
        assert phrase in text, phrase
    staged = probe.report([{**result, "passed": False}], spend=False)
    assert "## story - staged only, no model called" in staged and "PASS" not in staged and "Model cost" not in staged
