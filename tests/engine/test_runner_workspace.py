"""The pre-staged composition workspace: the video kit's, at job start, never a blocker."""

from __future__ import annotations

import json

from engine.agent.runner import _stage_composition_workspace


def test_stages_the_kit_workspace(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    warnings: list[str] = []
    _stage_composition_workspace(project, warnings.append)
    comp = project / "composition"
    for rel in ("edit.json", "package.json", "tsconfig.json", "src/index.ts", "src/scenes.ts",
                "src/look/tokens.ts", "src/look/look.tsx"):
        assert (comp / rel).is_file(), rel
    assert "@kit/engine" in (comp / "src" / "index.ts").read_text(encoding="utf-8")
    assert json.loads((comp / "edit.json").read_text(encoding="utf-8"))["main"] == []
    # Without symlink rights (plain Windows) the link alone warns and everything else stands;
    # a checkout that has no Node modules to link stays silent.
    assert all("node_modules" in warning for warning in warnings)


def test_skips_when_the_workspace_exists(tmp_path):
    project = tmp_path / "project"
    marker = project / "composition" / "src" / "existing.tsx"
    marker.parent.mkdir(parents=True)
    marker.write_text("keep me", encoding="utf-8")
    _stage_composition_workspace(project, lambda _m: None)
    assert marker.read_text(encoding="utf-8") == "keep me"
    # A project made before the kit keeps its hand-written composition untouched.
    assert not (project / "composition" / "edit.json").exists()
    assert not (project / "composition" / "package.json").exists()


def test_a_failure_warns_and_never_raises(tmp_path, monkeypatch):
    def broken(_project):
        raise RuntimeError("no kit here")
    monkeypatch.setattr("lib.videokit.stage.stage_workspace", broken)
    project = tmp_path / "project"
    project.mkdir()
    warnings: list[str] = []
    _stage_composition_workspace(project, warnings.append)
    assert warnings and "composition workspace pre-stage skipped" in warnings[0]
