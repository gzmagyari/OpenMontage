"""The video kit's contract lives where the director cannot miss it.

Two lessons shaped this file. A polite global guide section was once ignored outright (zero
Task calls), so the contract is binding and sits in the job brief, the stage skills and the
checkpoint protocol. And a director that hand-writes a composition spends more than five
minutes typing machinery while finished footage waits, so the machinery is the kit's and
these tests keep every binding phrase in place.
"""

from __future__ import annotations

from pathlib import Path

from engine.agent.brief import build_brief, build_revise_prompt

REPO = Path(__file__).resolve().parents[2]
SPOKE = REPO / "skills" / "pipelines" / "avatar-spokesperson"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _brief(**overrides) -> str:
    args = dict(prompt="probe prompt", references=[], project_dir=Path("/tmp/job_test123"),
                budget_usd=15.0, duration_seconds=30)
    args.update(overrides)
    return _flat(build_brief(**args))


def test_the_brief_binds_every_job_to_the_kit():
    # The brief reaches every job, whatever pipeline the guide routes the request to.
    brief = _brief()
    assert "Compose with the video kit" in brief
    assert "IS `/tmp/job_test123/composition/edit.json`" in brief.replace("\\", "/")
    assert "Never write sequencing, caption, transition or clip-playback code" in brief
    assert "never run Remotion by hand" in brief
    assert "Restyle the look" in brief
    for command in ("kit check --draft", "kit voice", "kit mix", "kit sheet", "kit render"):
        assert command in brief, command
    # Takes are made from the prepared audio the word timings belong to.
    assert "PREPARED audio" in brief and "`trim_silence` off" in brief


def test_the_burst_is_launched_the_way_a_job_can_launch_it():
    # Jobs run with the Bash tool's background mode off (one continuous turn), so the guide must
    # not send the director to it. The burst is a detached process the shell starts, and the
    # shell prints the last line however the script ends, so a crash cannot leave a job waiting.
    from engine.agent.session import cli_switches

    assert cli_switches()["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    guide = _read(REPO / "AGENT_GUIDE.md")
    asset = _read(SPOKE / "asset-director.md")
    compose = _read(SPOKE / "compose-director.md")
    for text in (guide, asset, compose):
        assert "run_in_background" not in text
        assert "background mode is switched off in jobs" in _flat(text)
    launch = """nohup sh -c 'python work/burst.py; echo "BURST EXIT $?"' > work/burst.out 2>&1 &"""
    wait = 'until grep -q "BURST EXIT" work/burst.out; do sleep 3; done'
    assert launch in guide and launch in asset
    assert wait in guide and wait in asset
    assert """nohup sh -c 'kit render --name <name>; echo "RENDER EXIT $?"' > work/render.out 2>&1 &""" in compose
    assert 'until grep -q "RENDER EXIT" work/render.out; do sleep 2; done' in compose


def test_the_kit_reference_covers_videos_without_a_presenter():
    # The reference is all a director has for a request the presenter skills do not cover.
    reference = _flat(_read(REPO / "remotion-composer" / "kit" / "README.md"))
    assert "leave `take` out for a voice-over that is only heard" in reference
    assert "A video with no voice has no lines: give every main item `for`" in reference
    assert "or the music alone when there is no voice" in reference
    # A line break written as two characters must stay two characters (it was once lost).
    assert 'statement; "\\n" starts a new line' in reference and '("\\n" starts a line)' in reference


def test_the_kit_replaces_the_render_step_of_every_pipeline():
    # Only the presenter pipeline's stage skills were rewritten for the kit. A request that
    # routes elsewhere (footage only, text only, a screen demo) meets stage skills that still
    # say `video_compose` and "choose a runtime", so the brief and the guide say which wins.
    brief = _brief()
    assert "whatever pipeline the guide routes the request to" in brief
    assert "choose a render runtime, assemble stock scene-types, hand-author a composition or render with `video_compose`, do this instead" in brief
    guide = _flat(_read(REPO / "AGENT_GUIDE.md"))
    assert "This holds for whatever pipeline the request routes to." in guide
    assert "the kit replaces that step" in guide and 'record `render_runtime: "remotion"` as given' in guide
    assert "The lock rules of a headless job are the same in every pipeline" in guide
    # The conflict is real: other pipelines' compose skills do name the old path.
    others = [p for p in (REPO / "skills" / "pipelines").glob("*/compose-director.md") if p.parent.name != "avatar-spokesperson"]
    assert len(others) >= 8 and sum("video_compose" in _read(p) for p in others) >= 4


def test_the_brief_turns_client_options_into_edit_settings():
    assert '"captions": {"enabled": false}' in _brief(captions=False)
    assert "name none in the edit" in _brief(music=False)
    assert "`music.file`" in _brief()
    # The music tool is named: video 713's batch first tried one that has no key here, and failed.
    assert "generate the bed with tool `fal_elevenlabs_music`" in _brief()
    assert "fal_elevenlabs_music" not in _brief(music=False)


def test_a_revision_goes_through_the_kit_when_the_project_has_an_edit():
    text = _flat(build_revise_prompt(feedback="Tighten the hook", project_dir=Path("/tmp/job_test123"), budget_usd=1.2))
    assert "composition/edit.json exists" in text.replace("\\", "/")
    assert "kit render --name" in text
    assert "Otherwise edit the existing composition code as it is" in text  # projects made before the kit
    assert "model h3max" in text and "h3max-acting for on-camera" not in text


def test_the_guide_makes_the_kit_the_mode_of_headless_jobs():
    guide = _read(REPO / "AGENT_GUIDE.md")
    flat = _flat(guide)
    assert "Headless video jobs use the video kit" in flat
    assert "MUST be restyled" in flat
    assert "The one-message spawn (MUST, at script lock" in flat
    assert "`composer`" in guide and "`mechanic`" in guide
    assert "the custom-scene author" in flat
    assert "never poll" in guide.lower()
    assert "is a protocol violation, not initiative: that code is the kit's" in flat
    # Subagent briefs carry absolute paths (their cwd is the read-only repo root).
    assert "ABSOLUTE under the project directory" in flat


def test_script_lock_demands_the_edit_the_shopping_list_and_the_spawn():
    text = _read(SPOKE / "script-director.md")
    assert "## Script Lock in Headless Jobs (Binding)" in text
    flat = _flat(text)
    assert "`composition/edit.json`" in flat and "`kit check --draft` must pass" in flat
    assert "A script lock without a passing draft check is INVALID" in flat
    assert "metadata.shopping_list" in text
    assert "one-message spawn" in text.lower()
    assert "hook plus 1-2 body takes" in text  # take discipline


def test_the_burst_is_bound_to_the_shopping_list_and_the_prepared_audio():
    text = _read(SPOKE / "asset-director.md")
    assert "### Shopping list (Binding)" in text
    assert "buys exactly" in text
    assert "Never poll" in text
    flat = _flat(text)
    assert "then `kit voice`, then takes" in flat
    assert "PREPARED audio" in flat and "`trim_silence` off" in flat


def test_edit_decisions_live_in_the_edit_and_the_kit_renders_it():
    edit = _flat(_read(SPOKE / "edit-director.md"))
    compose = _flat(_read(SPOKE / "compose-director.md"))
    assert "## Headless Jobs: Your Decisions Are the Edit (Binding)" in edit
    assert "`composition/edit.json`" in edit and "`kit sheet`" in edit
    assert "## Headless Jobs: Check, Sheet, Render (Binding)" in compose
    assert "Never run Remotion by hand" in compose
    assert "A compose checkpoint without a passing `kit check` is INVALID" in compose
    assert "delegation record" in compose


def test_the_checkpoints_require_the_kit_records():
    protocol = _flat(_read(REPO / "skills" / "meta" / "checkpoint-protocol.md"))
    for field in ("metadata.shopping_list", "metadata.kit", "metadata.delegation"):
        assert field in protocol, field
    assert "`kit check --draft`" in protocol
    assert "is a protocol violation: that code is the video kit's" in protocol
    # The preloaded digest in the guide carries the same requirements.
    guide = _read(REPO / "AGENT_GUIDE.md")
    for field in ("metadata.shopping_list", "metadata.kit", "metadata.delegation"):
        assert field in guide, field


def test_the_reports_reconcile_the_kit_record_and_spend():
    ep = _flat(_read(SPOKE / "executive-producer.md"))
    pub = _flat(_read(SPOKE / "publish-director.md"))
    for field in ("metadata.shopping_list", "metadata.kit", "metadata.delegation"):
        assert field in ep, field
    assert "the video came from `kit render`" in ep
    assert "kit record" in pub and "delegation record" in pub
    assert "never show cost, margin or multipliers" in pub
    # The report's first section is written for the client (see test_brief).
    assert "opens with `## Summary`, the client's section" in pub


def test_the_take_skill_agrees_with_the_brief_on_audio_and_the_default_model():
    # The brief says "read its skill and follow its audio contract": the two must not disagree.
    skill = _flat(_read(REPO / ".agents" / "skills" / "fal-avatar" / "SKILL.md"))
    assert '`model: "h3max"` (default)' in skill
    assert "with `trim_silence` off" in skill and "`kit voice`" in skill
    assert '"trim_silence": true' not in skill       # the example no longer trims a prepared file
    assert "$0.056/s at 768P" in skill and "+$0.20/s" in skill


def test_take_economics_default_native_with_the_lip_fix_upgrade():
    asset = _read(SPOKE / "asset-director.md")
    assert "### 1a. Take Model Economics (Binding)" in asset
    assert '`model: "h3max"`' in asset and "lip-fix mode" in asset
    brief = _brief()
    assert 'The default take model is `"h3max"`' in brief
    assert "lip-fix mode" in brief


def test_what_the_first_rehearsal_taught_is_written_down():
    """Four directors rehearsed against the kit (2026-11-10). Two wrote a word pinned to its
    line, as the reference says, and the checker refused it; one had its draft sheet crash on a
    backdrop that was still generating and made stand-in clips at the real paths; both that
    wrote a custom scene restyled the look first, then sent the composer."""
    guide = _flat(_read(REPO / "AGENT_GUIDE.md"))
    assert "is reported as still to write and does not block the lock" in guide
    assert "after the look, so the scenes can wear it" in guide
    assert "never make a stand-in file yourself" in guide
    script = _flat(_read(SPOKE / "script-director.md"))
    assert "does not block the lock" in script and "so they can wear the look" in script
    assert "never make a stand-in file yourself" in _flat(_read(SPOKE / "asset-director.md"))
    reference = _flat(_read(REPO / "remotion-composer" / "kit" / "README.md"))
    assert '`"line": "C"` (the word as line C says it)' in reference
    assert "Never put a placeholder file at a real path" in reference
    assert "The edit can name it before it is written" in reference
    assert '`archivoBlack` "Archivo Black"' in reference   # the family name tokens.ts needs
    # The guide's preloaded copy is the reference, word for word.
    assert reference in guide


def test_the_live_smoke_runs_the_launch_the_guide_gives():
    """No rehearsal can exercise the paid batch's launch, so the one-cent session smoke runs it
    with a stand-in batch. It must be the guide's own two commands, or it proves nothing."""
    import ast
    source = _read(REPO / "scripts" / "agent_session_smoke.py")
    constants = {node.targets[0].id: ast.literal_eval(node.value) for node in ast.parse(source).body
                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                 and node.targets[0].id in {"BATCH_LAUNCH", "BATCH_WAIT"}}
    assert set(constants) == {"BATCH_LAUNCH", "BATCH_WAIT"}
    guide = _read(REPO / "AGENT_GUIDE.md")
    asset = _read(SPOKE / "asset-director.md")
    for command in constants.values():
        assert command in guide, command
        assert command in asset, command
    assert "{BATCH_LAUNCH}" in source and "{BATCH_WAIT}" in source  # and the prompt really uses them
