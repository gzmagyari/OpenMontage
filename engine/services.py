"""Process-wide services shared by all jobs (one instance per API process)."""

from __future__ import annotations


class EngineServices:
    """Holds the shared stores runners need (bound by the app at startup)."""

    def __init__(self, settings):
        self.settings = settings
        self.assets = None
        self.store = None

    def bind(self, assets, store) -> None:
        self.assets, self.store = assets, store
