#!/usr/bin/env python3
"""Authoring rehearsal for the video kit: does the real director write a good edit?

The kit's tests prove that an edit renders. They cannot prove that the director, handed a prompt
it has never seen, writes its plan as an edit, restyles the look and stays out of the engine.
This runs the production session (the same brief, model, effort, box and guide as a paid job) as
far as a run can go without buying anything - everything before the first paid call, then the
work a real run does while its paid batch generates, against placeholders - for a few
deliberately different prompts, and reports what each one produced and what it took: model
calls, minutes, model cost. The prompt only adds where to stop; how to work has to come from
the brief, the guide and the skills, which is what is being tested.

It costs model calls only. Nothing can be bought:

  - each job's paid-generation budget is 0, so the tool desk refuses every paid call;
  - every provider credential in this process is replaced by a placeholder before anything
    else is imported, so a call that got past the desk would fail at the provider's door.
    The provider menu the director reads still shows what production shows.

Run it in a throwaway container of the image under test, never in the one serving jobs:

    docker run --rm --env-file .env \\
        --security-opt seccomp=docker/seccomp-openmontage.json \\
        --security-opt systempaths=unconfined --security-opt apparmor=unconfined \\
        -v kitprobe:/data -v <a folder>:/out -w /app --entrypoint python <image> \\
        scripts/kit_authoring_probe.py --prompts /out/prompts.json --out /out --spend

Without --spend it rehearses the rehearsal and calls no model: every job is staged, every brief
built, every box started, and the untouched workspace is reported.

A prompts file is a JSON list of {"key", "prompt"} objects; "seconds", "budget_usd" (the figure
the brief states, as the client app would send it), "language", "captions", "music" and "why"
are optional. Without one, two built-in prompts that name no product are used.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------- nothing can be bought

PLACEHOLDER = "rehearsal-no-key"
# What the session needs to reach its model. Every other credential is replaced.
LLM_CREDENTIALS = frozenset({"ANTHROPIC_API_KEY", "OPENMONTAGE_AGENT_LLM_UPSTREAM_KEY"})
# The box's own rule for "this name holds a credential" (engine/sandbox/box.py); a test keeps
# the two the same. It is repeated here so it can run before anything else is imported.
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|SECRETKEY|ACCESSKEY|PASSWORD|CREDENTIALS)$", re.I)


def neutralize_provider_keys(environ: Any = os.environ) -> list[str]:
    """Replace every provider credential with a placeholder; returns the names it replaced.
    A set name stays set, so a tool still reports its provider as configured, exactly as in
    production - but the value can open nothing."""
    replaced = []
    for name in sorted(environ):
        if name in LLM_CREDENTIALS or not _SECRET_NAME.search(name):
            continue
        if environ[name] and environ[name] != PLACEHOLDER:
            environ[name] = PLACEHOLDER
            replaced.append(name)
    return replaced


# ---------------------------------------------------------------- the prompts

@dataclasses.dataclass(frozen=True)
class Probe:
    key: str
    prompt: str
    seconds: float = 30.0
    budget_usd: float = 6.0  # the figure the brief states; this run's real budget is 0
    language: str = "en"
    captions: bool = True
    music: bool = True
    why: str = ""


DEFAULT_PROBES = (
    Probe("story", "show a dog running, then a new shot with a cat, then at the end add the same cat and dog "
                   "to the same video where they are friends",
          why="footage only: no product, no presenter, and two animals that must stay the same"),
    Probe("announcement", "Text-only announcement, no presenter and no voice-over: our shop is closed this "
                          "Saturday for stocktaking and opens again on Sunday at 10. Big animated text, upbeat music.",
          seconds=15, why="no footage and no voice: type and timing only"),
)

# Where the run stops, and nothing about how to work: the brief, the guide and the stage skills
# must carry that on their own, or the rehearsal would be testing this paragraph instead.
REHEARSAL = """
## This run is a rehearsal

It goes as far as a real run can go without buying anything, and stops there. Plan and prepare
exactly the video you would make with the budget above. No paid generation exists in this run
(every paid tool call is refused), so do not attempt one. Do everything you would do before the
first paid call. Then, instead of launching the paid batch, carry on with all the work you would
do while that batch is generating, with placeholders standing in for the files that do not exist
yet, and check that work the way you would check it in a real run. Stop there: no voice, no
takes, no images, no music, no final render, no exports and no RUN_REPORT.md. End with the
single line REHEARSAL DONE.
"""


def rehearsal_prompt(brief: str) -> str:
    return brief.rstrip() + "\n" + REHEARSAL


def load_probes(path: Path | None) -> list[Probe]:
    if path is None:
        return list(DEFAULT_PROBES)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("the prompts file must be a non-empty JSON list")
    fields = {f.name for f in dataclasses.fields(Probe)}
    probes = []
    for item in raw:
        if not isinstance(item, dict) or not {"key", "prompt"} <= set(item):
            raise ValueError('every prompt needs "key" and "prompt"')
        unknown = set(item) - fields
        if unknown:
            raise ValueError(f"unknown field(s) in the prompts file: {', '.join(sorted(unknown))}")
        probe = Probe(**item)
        if not re.fullmatch(r"[a-z0-9_]{1,24}", probe.key):
            raise ValueError(f"key {probe.key!r}: use lower-case letters, digits and underscores (at most 24)")
        if not probe.prompt.strip():
            raise ValueError(f"prompt {probe.key!r} is empty")
        probes.append(probe)
    keys = [p.key for p in probes]
    if len(set(keys)) != len(keys):
        raise ValueError("prompt keys must be unique")
    return probes


# ---------------------------------------------------------------- what the director left behind

# Staged by the runner and never the director's to change.
STAGED_FIXED = ("package.json", "tsconfig.json", "src/index.ts")
# Where a job's own files may be, relative to composition/.
_ALLOWED = re.compile(r"^(edit\.json|src/scenes\.ts|src/look/.+|src/scenes/.+)$")
# The engine's job, when found in a job's own code.
_ENGINE_MARKERS = {
    "sequences shots by hand": re.compile(r"<\s*(TransitionSeries|Series)\b|@remotion/transitions"),
    "plays the soundtrack by hand": re.compile(r"<\s*Audio\b"),
    "builds captions by hand": re.compile(r"@remotion/captions|createTikTokStyleCaptions"),
    "renders by hand": re.compile(r"\brender(Media|Still)\s*\("),
}
_REMOTION_BY_HAND = re.compile(r"\bremotion\s+(render|still|bundle|studio|compositions)\b|\brender(Media|Still)\s*\(")
_KIT_COMMAND = re.compile(r"(?:^|[\s;&|(`])(?:kit|python3?\s+-m\s+lib\.videokit)\s+"
                          r"(init|look|voice|check|sheet|mix|render|resolve|help)\b([^\n;&|`]*)")
_CODE = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_CODE_OR_DATA = (*_CODE, ".json", ".css", ".html", ".srt", ".vtt", ".txt", ".md", ".py", ".sh")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def fingerprint(project_dir: Path) -> dict[str, str]:
    """The staged files, hashed before the session touches anything."""
    comp = project_dir / "composition"
    return {rel: _sha(comp / rel) for rel in (*STAGED_FIXED, "edit.json") if (comp / rel).is_file()}


def _files(root: Path) -> list[Path]:
    """Every file under a folder, never following a link (composition/node_modules is one)."""
    found = []
    for folder, folders, names in os.walk(root, followlinks=False):
        folders[:] = sorted(name for name in folders if name != "node_modules" and not (Path(folder) / name).is_symlink())
        found.extend(Path(folder) / name for name in sorted(names))
    return found


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").split("\n")


def _changed_lines(a: list[str], b: list[str]) -> int:
    return sum(1 for line in difflib.ndiff(a, b) if line[:2] in ("+ ", "- "))


def workspace_facts(project_dir: Path, staged: dict[str, str]) -> dict[str, Any]:
    """What is in the job's composition folder now, against what was staged."""
    from lib.videokit import KIT_DIR
    from lib.videokit.stage import look_is_untouched, looks

    comp = project_dir / "composition"
    edit_file = comp / "edit.json"
    facts: dict[str, Any] = {"edit": {"exists": edit_file.is_file(), "written": False, "valid_json": False, "lines": 0}}
    if edit_file.is_file():
        facts["edit"]["written"] = _sha(edit_file) != staged.get("edit.json")
        facts["edit"]["lines"] = len([line for line in _lines(edit_file) if line.strip()])
        try:
            edit = json.loads(edit_file.read_text(encoding="utf-8"))
            facts["edit"]["valid_json"] = isinstance(edit, dict)
            if isinstance(edit, dict):
                facts["edit"]["shots"] = len(edit.get("main") or [])
                facts["edit"]["overlays"] = len(edit.get("overlays") or [])
                facts["edit"]["voice_lines"] = len((edit.get("voice") or {}).get("lines") or [])
                scenes = {str(item["scene"]) for item in (edit.get("main") or []) if isinstance(item, dict) and item.get("scene")}
                facts["edit"]["scenes_used"] = sorted(scenes)
        except ValueError:
            pass

    look_dir = comp / "src" / "look"
    look: dict[str, Any] = {"exists": look_dir.is_dir(), "untouched_starter": None, "closest_starter": None,
                            "token_lines_changed": 0, "files_changed": []}
    if look_dir.is_dir():
        look["untouched_starter"] = look_is_untouched(look_dir)
        tokens = look_dir / "tokens.ts"
        best: tuple[int, str] | None = None
        for name in looks():
            starter = KIT_DIR / "looks" / name / "tokens.ts"
            if tokens.is_file() and starter.is_file():
                changed = _changed_lines(_lines(starter), _lines(tokens))
                if best is None or changed < best[0]:
                    best = (changed, name)
        if best is not None:
            look["token_lines_changed"], look["closest_starter"] = best
            base = KIT_DIR / "looks" / best[1]
            look["files_changed"] = sorted(
                p.name for p in look_dir.iterdir()
                if p.is_file() and (not (base / p.name).is_file() or _sha(p) != _sha(base / p.name)))
    facts["look"] = look

    scenes_dir = comp / "src" / "scenes"
    facts["custom_scenes"] = []
    if scenes_dir.is_dir():
        for path in _files(scenes_dir):
            text = path.read_text(encoding="utf-8", errors="replace")
            imports = sorted({name.strip().split(" as ")[0] for block in re.findall(r"import\s*\{([^}]*)\}\s*from\s*'remotion'", text)
                              for name in block.split(",") if name.strip()})
            facts["custom_scenes"].append({"file": path.relative_to(comp).as_posix(),
                                           "lines": len([line for line in text.splitlines() if line.strip()]),
                                           "remotion": imports})

    unexpected, stray, markers = [], [], []
    for path in _files(comp) if comp.is_dir() else []:
        rel = path.relative_to(comp).as_posix()
        if rel in STAGED_FIXED:
            continue
        if not _ALLOWED.match(rel):
            # Code or data outside the edit, the look and the scenes is where hand-built
            # machinery lives; a picture or clip copied there is only untidy.
            (unexpected if path.suffix.lower() in _CODE_OR_DATA else stray).append(rel)
        if path.suffix in _CODE:
            text = path.read_text(encoding="utf-8", errors="replace")
            markers.extend(f"{rel}: {what}" for what, pattern in _ENGINE_MARKERS.items() if pattern.search(text))
    facts["unexpected_files"] = unexpected
    facts["stray_media"] = stray
    facts["engine_markers"] = markers
    facts["staged_files_changed"] = [rel for rel in STAGED_FIXED
                                     if not (comp / rel).is_file() or _sha(comp / rel) != staged.get(rel)]
    return facts


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        pass
    return rows


def _seconds_between(first: str | None, later: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(str(later)) - datetime.fromisoformat(str(first))).total_seconds()
    except (TypeError, ValueError):
        return None


def session_facts(agent_dir: Path, project_dir: Path) -> dict[str, Any]:
    """What the director did, from the job's journal (times, order) and transcript (whole inputs)."""
    journal = _read_jsonl(agent_dir / "journal.jsonl")
    transcript = _read_jsonl(agent_dir / "transcript.jsonl")
    inputs: dict[str, tuple[str, dict[str, Any]]] = {}
    outputs: dict[str, str] = {}  # whole tool results; the journal keeps only their first 2000 characters
    subagents, result = [], {}
    for row in transcript:
        kind = row.get("type")
        content = row.get("content")
        blocks: list[Any] = content if isinstance(content, list) else []
        if kind == "AssistantMessage":
            for block in blocks:
                if isinstance(block, dict) and block.get("name") and isinstance(block.get("input"), dict):
                    inputs[str(block.get("id"))] = (str(block["name"]), block["input"])
        elif kind == "UserMessage":
            for block in blocks:
                if isinstance(block, dict) and block.get("tool_use_id"):
                    body = block.get("content")
                    if isinstance(body, list):
                        body = "\n".join(str(part.get("text", "")) for part in body if isinstance(part, dict))
                    outputs[str(block["tool_use_id"])] = str(body or "")
        elif kind == "TaskStartedMessage":
            subagents.append({"type": row.get("data", {}).get("subagent_type"), "what": row.get("description")})
        elif kind == "ResultMessage":
            result = row

    results = {str(e.get("tool_use_id")): e for e in journal if e.get("kind") == "tool_result"}
    started = next((e.get("at") for e in journal if e.get("at")), None)
    ended = next((e.get("at") for e in reversed(journal) if e.get("at")), None)
    tools: dict[str, int] = {}
    kit_runs, by_hand, writes = [], [], {}
    first: dict[str, float | None] = {"edit_written_s": None, "check_passed_s": None, "sheet_made_s": None}
    project = str(project_dir)
    looked_at_sheet = False
    for name, tool_input in inputs.values():
        tools[name] = tools.get(name, 0) + 1
        if name == "Read" and re.search(r"work/kit/sheet\d*\.jpg$", str(tool_input.get("file_path") or "")):
            looked_at_sheet = True
    for entry in journal:
        if entry.get("kind") != "tool":
            continue
        name, tool_input = inputs.get(str(entry.get("id")), (str(entry.get("name")), {}))
        at = _seconds_between(started, entry.get("at"))
        if name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
            target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
            rel = target[len(project):].lstrip("/") if target.startswith(project) else target
            writes[rel] = writes.get(rel, 0) + 1
            if rel == "composition/edit.json" and first["edit_written_s"] is None:
                first["edit_written_s"] = at
        if name != "Bash":
            continue
        command = str(tool_input.get("command") or "")
        if _REMOTION_BY_HAND.search(command):
            by_hand.append(command[:200])
        # A shell command can write the edit too (a heredoc, a python one-liner).
        if first["edit_written_s"] is None and re.search(r">\s*\S*composition/edit\.json|edit\.json['\"]?\s*,\s*['\"]w", command):
            first["edit_written_s"] = at
        ran = [f"{m.group(1)}{' --draft' if '--draft' in m.group(2) else ''}" for m in _KIT_COMMAND.finditer(command)]
        if not ran:
            continue
        outcome = results.get(str(entry.get("id")), {})
        text = outputs.get(str(entry.get("id"))) or str(outcome.get("text") or "")
        ok: bool | None = None
        if "NOT OK" in text or re.search(r"^kit: ", text, re.M) or "Nothing was rendered" in text or "No sheet was made" in text:
            ok = False
        elif re.search(r"^OK: ", text, re.M) or "Sheet" in text:
            ok = True
        elif outcome.get("is_error"):
            ok = False
        done_at = _seconds_between(started, outcome.get("at")) if outcome else at
        kit_runs.append({"ran": ran, "ok": ok, "at_s": done_at})
        if ok and any(r.startswith("check") for r in ran) and first["check_passed_s"] is None:
            first["check_passed_s"] = done_at
        if ok and any(r.startswith("sheet") for r in ran) and first["sheet_made_s"] is None:
            first["sheet_made_s"] = done_at

    usage = result.get("usage") or {}
    return {
        "started": started, "ended": ended, "journal_seconds": _seconds_between(started, ended),
        "tools": dict(sorted(tools.items())),
        "kit_runs": kit_runs,
        "checks_before_the_first_pass": next(
            (i for i, run in enumerate([r for r in kit_runs if any(x.startswith("check") for x in r["ran"])]) if run["ok"]), None),
        "remotion_by_hand": by_hand,
        "looked_at_sheet": looked_at_sheet,
        "writes": dict(sorted(writes.items())),
        "subagents": subagents,
        "api_retries": sum(1 for e in journal if e.get("kind") in {"ratelimit", "system"}),
        **first,
        "tokens": {
            "input": usage.get("input_tokens"), "output": usage.get("output_tokens"),
            "thinking": (usage.get("output_tokens_details") or {}).get("thinking_tokens"),
            "cache_write": usage.get("cache_creation_input_tokens"), "cache_read": usage.get("cache_read_input_tokens"),
            "web_searches": (usage.get("server_tool_use") or {}).get("web_search_requests"),
        },
        "cost_by_model": {model: round(float((numbers or {}).get("costUSD") or 0), 4)
                          for model, numbers in (result.get("model_usage") or {}).items()},
        "said_done": "REHEARSAL DONE" in str(result.get("result") or ""),
    }


def gate(result: dict[str, Any]) -> dict[str, bool]:
    """The rehearsal's verdict for one prompt. The first three are the gate; the others are
    reported beside it."""
    work, session, check = result.get("workspace") or {}, result.get("session") or {}, result.get("check") or {}
    return {
        "the director wrote the edit and it passes `kit check --draft`":
            bool(work.get("edit", {}).get("written")) and check.get("ok") is True,
        "no engine code was written":
            not (work.get("unexpected_files") or work.get("engine_markers") or work.get("staged_files_changed")
                 or session.get("remotion_by_hand")),
        "a draft sheet renders": bool(result.get("sheets")),
        "the look was restyled": bool(work.get("look", {}).get("exists")) and work.get("look", {}).get("untouched_starter") is None,
        "the director made a draft sheet and looked at it":
            bool(result.get("sheets_by_the_director")) and bool(session.get("looked_at_sheet")),
        "nothing was bought and nothing paid was attempted":
            not result.get("spent_usd") and not result.get("paid_attempts"),
    }


GATE_KEYS = 3  # the first three checks of gate() decide


# ---------------------------------------------------------------- one rehearsal

def kit(project_dir: Path, *args: str, timeout: int = 420) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}".rstrip(os.pathsep)}
    done = subprocess.run([sys.executable, "-m", "lib.videokit", *args, "--project", str(project_dir)],
                          cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=timeout)
    return done.returncode, (done.stdout + done.stderr).strip()


def _copy(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("node_modules"))
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def collect(result: dict[str, Any], project_dir: Path, agent_dir: Path, out_dir: Path) -> None:
    """The files a person reads to judge the rehearsal, next to its numbers."""
    from lib.videokit import KIT_DIR

    out_dir.mkdir(parents=True, exist_ok=True)
    comp = project_dir / "composition"
    for source, name in ((agent_dir / "brief.md", "brief.md"), (agent_dir / "journal.jsonl", "journal.jsonl"),
                         (agent_dir / "transcript.jsonl", "transcript.jsonl"),
                         (project_dir / "artifacts" / "video_plan.md", "video_plan.md"),
                         (comp / "edit.json", "edit.json"), (comp / "src" / "scenes.ts", "scenes.ts"),
                         (comp / "src" / "look", "look"), (comp / "src" / "scenes", "scenes"),
                         (project_dir / "work" / "kit" / "check.json", "check.json")):
        _copy(source, out_dir / name)
    for sheet in result.get("sheets") or []:
        _copy(project_dir / sheet, out_dir / Path(sheet).name)
    starter = (result.get("workspace") or {}).get("look", {}).get("closest_starter")
    tokens = comp / "src" / "look" / "tokens.ts"
    if starter and tokens.is_file():
        diff = difflib.unified_diff(_lines(KIT_DIR / "looks" / starter / "tokens.ts"), _lines(tokens),
                                    f"starter/{starter}/tokens.ts", "this video/tokens.ts", lineterm="")
        (out_dir / "look_tokens.diff").write_text("\n".join(diff) + "\n", encoding="utf-8")
    (out_dir / "result.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")


async def rehearse(probe: Probe, *, settings: Any, data_dir: Path, stamp: str, spend: bool, cap_usd: float,
                   wall_minutes: float, out_dir: Path, say: Callable[[str], None]) -> dict[str, Any]:
    # Imported here: by now the provider credentials are placeholders.
    from engine.agent.brief import build_brief
    from engine.agent.runner import _stage_composition_workspace, _write_provider_menu
    from engine.agent.session import AgentSession, anthropic_api_key, cli_switches
    from engine.sandbox.box import AgentBox
    from lib import job_ledger

    job = f"job_rehearsal_{probe.key}_{stamp}"
    agent_dir = data_dir / "jobs" / job / "agent"
    project_dir = data_dir / "projects" / job
    (project_dir / "refs").mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    prompt = rehearsal_prompt(build_brief(
        prompt=probe.prompt, references=[], project_dir=project_dir, budget_usd=probe.budget_usd,
        duration_seconds=probe.seconds, language=probe.language, captions=probe.captions, music=probe.music))
    (agent_dir / "brief.md").write_text(prompt, encoding="utf-8")
    # Staged exactly as the runner stages a job.
    await _write_provider_menu(project_dir, warnings.append)
    _stage_composition_workspace(project_dir, warnings.append)
    staged = fingerprint(project_dir)
    job_ledger.write_budget(agent_dir, 0.0)  # the desk refuses every paid call
    (project_dir / ".agent_session").mkdir(parents=True, exist_ok=True)

    model = settings.agent_model
    upstream = settings.agent_llm_upstream
    result: dict[str, Any] = {"probe": probe.key, "why": probe.why, "prompt": probe.prompt, "job": job,
                              "project_dir": str(project_dir), "model": model, "effort": settings.agent_effort,
                              "called_the_model": spend, "stopped": None}
    turns = 0

    def on_event(entry: dict[str, Any]) -> None:
        nonlocal turns
        if entry.get("kind") == "tool":
            turns += 1
            if turns % 10 == 0:
                say(f"[{probe.key}] {turns} tool calls")

    started = time.monotonic()
    outcome = None
    os.environ.setdefault("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    switches = cli_switches()
    if upstream:
        switches.setdefault("ANTHROPIC_SMALL_FAST_MODEL", "claude-haiku-5-5")
        switches.setdefault("ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-5-5")
    async with AgentBox(agent_dir=agent_dir, project_dir=project_dir, model=model,
                        claude_cap_usd=cap_usd * (1.0 if upstream else 1.5), cli_switches=switches,
                        llm_upstream=upstream, warn=warnings.append,
                        api_key=(settings.agent_llm_upstream_key if upstream else
                                 (anthropic_api_key(REPO_ROOT) if spend else "no-model-is-called"))) as box:
        if spend:
            session = AgentSession(
                repo_root=REPO_ROOT, journal_path=agent_dir / "journal.jsonl",
                transcript_path=agent_dir / "transcript.jsonl", model=model, effort=settings.agent_effort,
                max_turns=settings.agent_max_turns, max_claude_usd=cap_usd, composer_model=settings.composer_model,
                on_event=on_event, cli_path=box.cli_path, box_env=box.session_env(), settings_json=box.settings_json)
            try:
                outcome = await asyncio.wait_for(session.run(prompt), timeout=wall_minutes * 60)
            except asyncio.TimeoutError:
                result["stopped"] = f"still working after {wall_minutes:g} minutes"
            except Exception as exc:  # one prompt's failure must not lose the others' results
                result["stopped"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        result["gateway_usd"] = round(box.gateway.spend_usd, 4) if box.gateway else 0.0
    result["wall_seconds"] = round(time.monotonic() - started, 1)
    if outcome is not None:
        result.update(succeeded=outcome.succeeded, subtype=outcome.subtype, model_calls=outcome.num_turns,
                      llm_usd=round(outcome.total_cost_usd, 4), session_seconds=round(outcome.duration_ms / 1000, 1))
        if not outcome.succeeded and not result["stopped"]:
            result["stopped"] = f"the session ended as {outcome.subtype}"

    ledger = job_ledger.summarize(job_ledger.read_entries(agent_dir))
    result["spent_usd"] = round(ledger.spent_usd, 4)
    result["paid_attempts"] = [str(d.get("tool")) for d in ledger.denials]
    result["warnings"] = warnings
    result["workspace"] = workspace_facts(project_dir, staged)
    result["session"] = session_facts(agent_dir, project_dir)

    # The verdict is the probe's own, not the director's word for it.
    work = project_dir / "work" / "kit"
    result["sheets_by_the_director"] = sorted(p.relative_to(project_dir).as_posix() for p in work.glob("sheet*.jpg"))
    if (work / "check.json").is_file():
        _copy(work / "check.json", out_dir / probe.key / "check_as_the_director_left_it.json")
    code, text = await asyncio.to_thread(kit, project_dir, "check", "--draft")
    try:
        result["check"] = json.loads((work / "check.json").read_text(encoding="utf-8"))["summary"] if code in (0, 1) else {}
    except (OSError, ValueError, KeyError):
        result["check"] = {}
    result["check"] = {**result["check"], "exit": code, "report": text[-1500:]}
    result["sheets"] = list(result["sheets_by_the_director"])
    if spend and not result["sheets"] and result["check"].get("ok"):
        code, text = await asyncio.to_thread(kit, project_dir, "sheet", "--draft")
        result["sheets"] = sorted(p.relative_to(project_dir).as_posix() for p in work.glob("sheet*.jpg"))
        result["sheet_made_by_the_probe"] = {"exit": code, "report": text[-800:]}
    result["gate"] = gate(result)
    result["passed"] = all(list(result["gate"].values())[:GATE_KEYS])
    await asyncio.to_thread(collect, result, project_dir, agent_dir, out_dir / probe.key)
    return result


# ---------------------------------------------------------------- the report

def _minutes(seconds: Any) -> str:
    try:
        return f"{int(float(seconds) // 60)}:{int(float(seconds) % 60):02d}"
    except (TypeError, ValueError):
        return "-"


def report(results: list[dict[str, Any]], spend: bool) -> str:
    lines = []
    for r in results:
        work, session = r.get("workspace") or {}, r.get("session") or {}
        look = work.get("look") or {}
        if spend:
            lines.append(f"## {r['probe']} - {'PASSED' if r.get('passed') else 'NOT PASSED'}")
        else:
            lines.append(f"## {r['probe']} - staged only, no model called")
        if r.get("why"):
            lines.append(str(r["why"]))
        lines.append(f"> {r['prompt']}")
        lines.append("")
        for name, ok in (r.get("gate") or {}).items() if spend else ():
            lines.append(f"- {'PASS' if ok else 'FAIL'}  {name}")
        if r.get("stopped"):
            lines.append(f"- STOPPED: {r['stopped']}")
        edit = work.get("edit") or {}
        check = r.get("check") or {}
        used = ", ".join(edit.get("scenes_used") or []) or "none"
        lines.append(f"- edit: {edit.get('lines', 0)} lines, {edit.get('shots', 0)} shots, {edit.get('overlays', 0)} overlays, "
                     f"{edit.get('voice_lines', 0)} voice lines; scenes used: {used}")
        lines.append(f"- check --draft: {check.get('errors', '?')} error(s), {check.get('warnings', '?')} warning(s), "
                     f"{check.get('seconds', '?')} s; still to generate: {len(check.get('pending') or [])} file(s)")
        state = f"UNTOUCHED {look.get('untouched_starter')}" if look.get("untouched_starter") else "restyled"
        changed = ", ".join(look.get("files_changed") or []) or "none"
        lines.append(f"- look: {state} (from {look.get('closest_starter')}, {look.get('token_lines_changed', 0)} "
                     f"token lines changed, files changed: {changed})")
        scenes = ", ".join(f"{s.get('file')} ({s.get('lines')} lines)" for s in work.get("custom_scenes") or []) or "none"
        lines.append(f"- custom scenes: {scenes}")
        for label, key in (("unexpected code or data files", "unexpected_files"),
                           ("media copied into composition/ (untidy, not engine work)", "stray_media"),
                           ("engine work in job code", "engine_markers"),
                           ("staged files changed", "staged_files_changed")):
            if work.get(key):
                lines.append(f"- {label}: {', '.join(work[key])}")
        if session.get("remotion_by_hand"):
            lines.append(f"- ran Remotion by hand: {session['remotion_by_hand'][:3]}")
        if spend:
            marks = {True: "", False: " (failed)", None: " (?)"}
            runs = ", ".join("+".join(run["ran"]) + marks[run["ok"]] for run in session.get("kit_runs") or []) or "none"
            waits = f", {session['api_retries']} API retry or rate-limit note(s)" if session.get("api_retries") else ""
            lines.append(f"- took: {r.get('model_calls', '?')} model calls, {_minutes(r.get('wall_seconds'))} wall, "
                         f"model cost ${r.get('llm_usd', 0):.2f} (gateway meter ${r.get('gateway_usd', 0):.2f}){waits}")
            lines.append(f"- edit first written at {_minutes(session.get('edit_written_s'))}, first passing check at "
                         f"{_minutes(session.get('check_passed_s'))}, first sheet at {_minutes(session.get('sheet_made_s'))}; "
                         f"kit runs: {runs}")
            tokens = session.get("tokens") or {}
            lines.append(f"- tokens: {tokens.get('output')} out ({tokens.get('thinking')} thinking), {tokens.get('cache_write')} "
                         f"cache write, {tokens.get('cache_read')} cache read; tools: {session.get('tools')}; "
                         f"subagents: {len(session.get('subagents') or [])}")
        if r.get("paid_attempts"):
            lines.append(f"- paid calls attempted and refused: {', '.join(r['paid_attempts'])}")
        if r.get("warnings"):
            lines.append(f"- warnings: {'; '.join(r['warnings'])[:400]}")
        lines.append("")
    if spend:
        total = sum(float(r.get("llm_usd") or 0) for r in results)
        metered = sum(float(r.get("gateway_usd") or 0) for r in results)
        lines.append(f"Model cost in total: ${total:.2f} (gateway meters: ${metered:.2f}). Paid generation: "
                     f"${sum(float(r.get('spent_usd') or 0) for r in results):.2f}.")
    return "\n".join(lines)


async def main_async(args: argparse.Namespace) -> int:
    replaced = neutralize_provider_keys()
    # Only now: nothing below can see a real provider credential.
    from engine.config import load_settings
    from engine.sandbox.box import sandbox_problem

    probes = load_probes(Path(args.prompts) if args.prompts else None)
    if args.only:
        wanted = {key.strip() for key in args.only.split(",") if key.strip()}
        missing = wanted - {p.key for p in probes}
        if missing:
            print(f"no prompt named: {', '.join(sorted(missing))}")
            return 2
        probes = [p for p in probes if p.key in wanted]
    settings = load_settings()
    print(f"{len(probes)} prompt(s): {', '.join(p.key for p in probes)}")
    print(f"model {settings.agent_model}, effort {settings.agent_effort or 'default'}, composer {settings.composer_model}"
          f"{', through a custom upstream' if settings.agent_llm_upstream else ''}")
    print(f"provider credentials replaced by a placeholder: {len(replaced)}; paid-generation budget: $0.00")
    if args.spend:
        print(f"CALLING THE MODEL: at most ${args.cap:.2f} a prompt, ${args.cap * len(probes):.2f} in all "
              f"(the gateway stops a prompt at ${args.cap * (1.0 if settings.agent_llm_upstream else 1.5):.2f})")
    else:
        print("no model is called (pass --spend for the real rehearsal)")
    problem = sandbox_problem()
    if problem:
        print(f"FAIL  this host cannot build agent boxes: {problem}")
        return 1
    if settings.agent_sandbox != "required":
        print("FAIL  OPENMONTAGE_AGENT_SANDBOX is not 'required': production jobs are boxed, so is the rehearsal")
        return 1

    stamp = datetime.now(timezone.utc).strftime("%m%d%H%M%S")
    out_dir = Path(args.out) / f"rehearsal_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    gate_open = asyncio.Semaphore(max(1, args.parallel))

    async def one(probe: Probe) -> dict[str, Any]:
        async with gate_open:
            print(f"[{probe.key}] started", flush=True)
            try:
                result = await rehearse(probe, settings=settings, data_dir=settings.data_dir, stamp=stamp,
                                        spend=args.spend, cap_usd=args.cap, wall_minutes=args.wall, out_dir=out_dir,
                                        say=lambda text: print(text, flush=True))
            except Exception as exc:
                result = {"probe": probe.key, "why": probe.why, "prompt": probe.prompt, "passed": False,
                          "stopped": f"{type(exc).__name__}: {str(exc)[:400]}", "gate": {}}
            verdict = ("passed" if result.get("passed") else "not passed") if args.spend else "staged"
            print(f"[{probe.key}] finished: {verdict}"
                  f"{' - ' + str(result['stopped']) if result.get('stopped') else ''}", flush=True)
            return result

    results = await asyncio.gather(*(one(p) for p in probes))
    text = report(list(results), args.spend)
    (out_dir / "summary.md").write_text(text + "\n", encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(list(results), indent=1, default=str), encoding="utf-8")
    print()
    print(text)
    print(f"\nEverything is in {out_dir}")
    if not args.spend:
        # The plumbing check: every job staged, every box started, every untouched workspace read.
        staged_ok = all(r.get("workspace", {}).get("look", {}).get("untouched_starter") and not r.get("stopped")
                        and not r.get("workspace", {}).get("staged_files_changed") for r in results)
        print("PASS  every job was staged and boxed" if staged_ok else "FAIL  a job could not be staged or boxed")
        return 0 if staged_ok else 1
    return 0 if all(r.get("passed") for r in results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--prompts", help="a JSON file of prompts (default: two built-in ones)")
    parser.add_argument("--only", help="comma-separated prompt keys to run")
    parser.add_argument("--out", default="/data/kit_rehearsals", help="where the results folder is written")
    parser.add_argument("--spend", action="store_true", help="call the model (without it nothing is spent)")
    parser.add_argument("--cap", type=float, default=1.5, help="most model spend for one prompt, in USD (default 1.50)")
    parser.add_argument("--wall", type=float, default=25.0, help="minutes one prompt may take (default 25)")
    parser.add_argument("--parallel", type=int, default=4, help="prompts rehearsed at the same time (default 4)")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
