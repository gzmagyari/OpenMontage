"""The preflight provider-menu file: written at job start, never a blocker."""

from __future__ import annotations

import asyncio
import json
import sys
import types

from engine.agent.runner import _write_provider_menu


def _stub_registry(monkeypatch, registry_obj) -> None:
    mod = types.ModuleType("tools.tool_registry")
    mod.registry = registry_obj
    pkg = types.ModuleType("tools")
    pkg.tool_registry = mod
    monkeypatch.setitem(sys.modules, "tools", pkg)
    monkeypatch.setitem(sys.modules, "tools.tool_registry", mod)


def test_writes_the_menu_file_with_the_summary_shape(tmp_path, monkeypatch):
    class Registry:
        def discover(self):
            pass

        def provider_menu_summary(self):
            return {"composition_runtimes": {"ffmpeg": True}, "capabilities": []}

    _stub_registry(monkeypatch, Registry())
    warnings: list[str] = []
    asyncio.run(_write_provider_menu(tmp_path, warnings.append))
    data = json.loads((tmp_path / "work" / "provider_menu.json").read_text(encoding="utf-8"))
    # The file is a drop-in for provider_menu_summary() output, plus a provenance marker.
    assert data["composition_runtimes"] == {"ffmpeg": True}
    assert "_generated" in data
    assert warnings == []


def test_a_failing_registry_warns_and_never_raises(tmp_path, monkeypatch):
    class Registry:
        def discover(self):
            raise RuntimeError("registry exploded")

        def provider_menu_summary(self):  # pragma: no cover - discover raises first
            return {}

    _stub_registry(monkeypatch, Registry())
    warnings: list[str] = []
    asyncio.run(_write_provider_menu(tmp_path, warnings.append))
    assert not (tmp_path / "work" / "provider_menu.json").exists()
    assert warnings and "provider-menu preflight file skipped" in warnings[0]
