"""The pre-staged composition workspace: boilerplate at job start, never a blocker."""

from __future__ import annotations

import os

import engine.agent.runner as runner
from engine.agent.runner import _stage_composition_workspace


def _can_symlink(tmp_path) -> bool:
    try:
        (tmp_path / "_link_probe").symlink_to(tmp_path)
        return True
    except OSError:
        return False


def _fake_repo(tmp_path):
    source = tmp_path / "repo" / "remotion-composer"
    (source / "node_modules").mkdir(parents=True)
    (source / "package.json").write_text("{}", encoding="utf-8")
    (source / "tsconfig.json").write_text("{}", encoding="utf-8")
    return tmp_path / "repo"


def test_stages_dirs_configs_and_link(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_repo_root", lambda: _fake_repo(tmp_path))
    project = tmp_path / "project"
    project.mkdir()
    warnings: list[str] = []
    _stage_composition_workspace(project, warnings.append)
    comp = project / "composition"
    assert (comp / "src").is_dir() and (comp / "public").is_dir()
    assert (comp / "package.json").is_file() and (comp / "tsconfig.json").is_file()
    if _can_symlink(tmp_path):
        assert os.path.islink(comp / "node_modules")
        assert warnings == []
    # Without symlink support (plain Windows), the link alone warns and everything else stands.


def test_skips_when_the_workspace_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "_repo_root", lambda: _fake_repo(tmp_path))
    project = tmp_path / "project"
    marker = project / "composition" / "src" / "existing.tsx"
    marker.parent.mkdir(parents=True)
    marker.write_text("keep me", encoding="utf-8")
    _stage_composition_workspace(project, lambda _m: None)
    assert marker.read_text(encoding="utf-8") == "keep me"
    assert not (project / "composition" / "package.json").exists()


def test_a_failure_warns_and_never_raises(tmp_path, monkeypatch):
    def broken_root():
        raise RuntimeError("no repo here")
    monkeypatch.setattr(runner, "_repo_root", broken_root)
    project = tmp_path / "project"
    project.mkdir()
    warnings: list[str] = []
    _stage_composition_workspace(project, warnings.append)
    assert warnings and "composition workspace pre-stage skipped" in warnings[0]
