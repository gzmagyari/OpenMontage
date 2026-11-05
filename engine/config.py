"""Runtime configuration for the video-generator API. Everything is env-driven."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from lib.paths import PROJECTS_DIR, REPO_ROOT


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _str(name: str, default: str) -> str:
    value = _env(name)
    return value if value is not None else default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None or raw.strip().lower() == "auto":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw is not None else default
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    projects_dir: Path
    api_token: str | None
    max_active_jobs: int
    max_queued_jobs: int
    render_concurrency: int
    default_max_budget_usd: float
    job_ttl_hours: float
    enable_tool_api: bool
    max_upload_mb: int
    agent_model: str
    agent_max_turns: int
    agent_wall_time_minutes: float
    agent_max_claude_usd: float | None
    agent_concurrency: int

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.jobs_dir, self.assets_dir, self.projects_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    cpus = os.cpu_count() or 2
    return Settings(
        data_dir=Path(_str("OPENMONTAGE_DATA_DIR", str(REPO_ROOT / "data"))),
        projects_dir=PROJECTS_DIR,
        api_token=_env("OPENMONTAGE_API_TOKEN"),
        max_active_jobs=_int("MAX_ACTIVE_JOBS", 20),
        max_queued_jobs=_int("MAX_QUEUED_JOBS", 100),
        # Remotion renders are CPU-bound (one render saturates ~8 cores); everything else mostly waits.
        render_concurrency=_int("RENDER_CONCURRENCY", max(1, cpus // 8)),
        default_max_budget_usd=_float("DEFAULT_MAX_BUDGET_USD", 10.0),
        job_ttl_hours=_float("JOB_TTL_HOURS", 72.0),
        enable_tool_api=_bool("OPENMONTAGE_ENABLE_TOOL_API", False),
        max_upload_mb=_int("MAX_UPLOAD_MB", 100),
        agent_model=_str("OPENMONTAGE_AGENT_MODEL", "claude-haiku-5-5"),
        agent_max_turns=_int("AGENT_MAX_TURNS", 500),
        agent_wall_time_minutes=_float("AGENT_WALL_TIME_MINUTES", 45.0),
        # The agent-loop (Claude) spend cap per session; generation spend is capped by the brief.
        agent_max_claude_usd=_float("AGENT_MAX_CLAUDE_USD", 3.0),
        agent_concurrency=_int("AGENT_CONCURRENCY", 10),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
