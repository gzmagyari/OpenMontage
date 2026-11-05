"""Per-job execution context handed to pipeline runners."""

from __future__ import annotations

import logging
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

from engine.jobs.models import CostLine, Job

if TYPE_CHECKING:
    from engine.config import Settings
    from engine.jobs.limits import Limits
    from engine.jobs.store import JobStore

log = logging.getLogger("openmontage.jobs")


class JobFailed(Exception):
    """Raise from a runner to fail the job with a stable, client-facing error code."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class JobContext:
    def __init__(self, job: Job, store: "JobStore", limits: "Limits", settings: "Settings", services=None):
        self.job = job
        self.store = store
        self.limits = limits
        self.settings = settings
        self.services = services  # shared engine services (asr pool, fal client, ...)

    # -- locations -------------------------------------------------------------
    @property
    def job_dir(self) -> Path:
        return self.store.job_dir(self.job.id)

    @property
    def files_dir(self) -> Path:
        path = self.store.files_dir(self.job.id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def work_dir(self) -> Path:
        path = self.job_dir / "work"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def project_id(self) -> str:
        return self.job.id

    @property
    def project_dir(self) -> Path:
        return self.settings.projects_dir / self.project_id

    # -- progress ----------------------------------------------------------------
    def set_stage(self, stage: str, detail: str | None = None, progress: int | None = None) -> None:
        self.job.stage = stage
        self.job.stage_detail = detail
        if progress is not None:
            self.job.progress = max(self.job.progress, min(99, int(progress)))
        self.store.save(self.job, force=True)
        self.log(f"stage={stage} detail={detail} progress={self.job.progress}")

    def set_progress(self, progress: float, detail: str | None = None) -> None:
        self.job.progress = max(self.job.progress, min(99, int(progress)))
        if detail is not None:
            self.job.stage_detail = detail
        self.store.save(self.job)

    def set_detail(self, detail: str | None) -> None:
        self.job.stage_detail = detail
        self.store.save(self.job)

    @asynccontextmanager
    async def resource(self, name: str, label: str | None = None) -> AsyncIterator[None]:
        """Wait (FIFO) for a slot of a limited resource; report the place in line meanwhile."""
        label = label or name
        restore = self.job.stage_detail

        def on_position(position: int | None) -> None:
            if position:
                self.set_detail(f"waiting for {label} slot (#{position} in line)")
            elif self.job.stage_detail and self.job.stage_detail.startswith("waiting for"):
                self.set_detail(restore)

        async with self.limits.get(name).acquire(on_position):
            yield

    # -- money -------------------------------------------------------------------
    @property
    def max_budget_usd(self) -> float:
        options = self.job.request.get("options") or {}
        budget = options.get("max_budget_usd")
        return float(budget) if budget is not None else self.settings.default_max_budget_usd

    def add_cost(self, item: str, usd: float, provider: str | None = None, estimated: bool = False) -> None:
        if usd <= 0:
            return
        self.job.cost.lines.append(CostLine(item=item, usd=round(usd, 4), provider=provider, estimated=estimated))
        self.job.cost.spent_usd = round(self.job.cost.spent_usd + usd, 4)
        self.store.save(self.job)

    def ensure_budget(self, additional_usd: float, what: str) -> None:
        if self.job.cost.spent_usd + additional_usd > self.max_budget_usd + 1e-9:
            raise JobFailed(
                "budget_exceeded",
                f"{what} would cost ${additional_usd:.2f}; spent ${self.job.cost.spent_usd:.2f} "
                f"of the ${self.max_budget_usd:.2f} budget.",
                retryable=False,
            )

    # -- diagnostics ---------------------------------------------------------------
    def warn(self, message: str) -> None:
        if message not in self.job.warnings:
            self.job.warnings.append(message)
            self.store.save(self.job)
        self.log(f"WARNING {message}")

    def log(self, message: str) -> None:
        try:
            self.job_dir.mkdir(parents=True, exist_ok=True)
            with open(self.job_dir / "job.log", "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
        except OSError:
            pass
        log.debug("[%s] %s", self.job.id, message)

    def log_exception(self, exc: BaseException) -> None:
        self.log("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
