"""Job manager: bounded admission queue + asyncio runner + recovery + cleanup.

Admission: up to ``max_active_jobs`` run concurrently; extra jobs wait in an
internal FIFO queue (status ``queued`` with a queue position). Only when that
queue holds ``max_queued_jobs`` does ``submit`` raise ``QueueFull`` (HTTP 429)
- before any work or spend happens.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import shutil
from collections import deque
from datetime import timedelta
from typing import Any, Awaitable, Callable

from engine.jobs.context import JobContext, JobFailed
from engine.jobs.limits import Limits
from engine.jobs.models import Job, JobError, JobKind, JobStatus, TERMINAL_STATUSES, new_id, utcnow
from engine.jobs.store import JobStore

log = logging.getLogger("openmontage.jobs")

Runner = Callable[[JobContext], Awaitable[dict[str, Any]]]


class QueueFull(Exception):
    pass


class NoRunner(Exception):
    """This deployment has no runner for the requested job kind."""


class JobManager:
    def __init__(self, settings, store: JobStore, limits: Limits, runners: dict[JobKind, Runner], services=None):
        self.settings = settings
        self.store = store
        self.limits = limits
        self.runners = runners
        self.services = services
        self._queue: deque[str] = deque()
        self._running: dict[str, asyncio.Task] = {}
        self._stopping = False
        self._cleanup_task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        jobs = sorted(self.store.load_all(), key=lambda j: j.created_at)
        for job in jobs:
            if job.status == JobStatus.RUNNING:
                # Interrupted by a restart: resume. Stages are idempotent and paid
                # assets come from the content-addressed cache, so nothing is re-bought.
                job.status = JobStatus.QUEUED
                job.stage_detail = "resuming after restart"
                self.store.save(job, force=True)
            if job.status == JobStatus.QUEUED:
                self._queue.append(job.id)
        self._update_positions()
        self._kick()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._cleanup_task, self._flush_task):
            if task:
                task.cancel()
        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)  # let done-callbacks persist interrupted jobs as queued
        self.store.flush()

    # -- API -----------------------------------------------------------------------
    def submit(
        self,
        kind: JobKind,
        request: dict[str, Any],
        idempotency_key: str | None = None,
        parent_job_id: str | None = None,
    ) -> tuple[Job, bool]:
        """Create (or return the existing idempotent) job. Returns (job, created)."""
        if kind not in self.runners:
            raise NoRunner(f"no runner registered for job kind {kind.value!r}")
        if idempotency_key:
            existing = self.store.find_idempotent(idempotency_key)
            if existing is not None:
                return existing, False
        if len(self._running) >= self.settings.max_active_jobs and len(self._queue) >= self.settings.max_queued_jobs:
            raise QueueFull(
                f"{len(self._running)} jobs running and {len(self._queue)} queued (limit "
                f"{self.settings.max_queued_jobs}); retry later"
            )
        job = Job(
            id=new_id("job"),
            kind=kind,
            request=request,
            idempotency_key=idempotency_key,
            parent_job_id=parent_job_id,
            stage="queued",
        )
        self.store.add(job)
        self._queue.append(job.id)
        self._update_positions()
        self._kick()
        return job, True

    def cancel(self, job_id: str) -> Job | None:
        job = self.store.get(job_id)
        if job is None or job.is_terminal:
            return job
        if job_id in self._queue:
            self._queue.remove(job_id)
            self._finish(job, JobStatus.CANCELLED, JobError(code="cancelled", message="Cancelled before start", retryable=True))
            self._update_positions()
        elif job_id in self._running:
            job.stage_detail = "cancelling"
            self.store.save(job, force=True)
            self._running[job_id].cancel()
        return job

    def stats(self) -> dict[str, Any]:
        return {
            "running": len(self._running),
            "queued": len(self._queue),
            "max_active_jobs": self.settings.max_active_jobs,
            "max_queued_jobs": self.settings.max_queued_jobs,
            "limits": self.limits.stats(),
        }

    # -- internals -------------------------------------------------------------------
    def _update_positions(self) -> None:
        for position, job_id in enumerate(self._queue, start=1):
            job = self.store.get(job_id)
            if job is not None and job.queue_position != position:
                job.queue_position = position
                job.stage_detail = f"waiting for a job slot (#{position} in line)"
                self.store.save(job)

    def _kick(self) -> None:
        if self._stopping:
            return
        while self._queue and len(self._running) < self.settings.max_active_jobs:
            job_id = self._queue.popleft()
            job = self.store.get(job_id)
            if job is None or job.is_terminal:
                continue
            # Mark running immediately so a poll right after submit is accurate.
            job.status = JobStatus.RUNNING
            job.queue_position = None
            job.started_at = job.started_at or utcnow()
            if job.stage == "queued":
                job.stage = "starting"
            if job.stage_detail and job.stage_detail.startswith("waiting for a job slot"):
                job.stage_detail = None
            self.store.save(job, force=True)
            task = asyncio.create_task(self._run(job), name=f"job:{job_id}")
            self._running[job_id] = task
            # The callback (not the coroutine) owns slot release: a task cancelled before its
            # first step never runs its own except/finally, and must not leak a slot.
            task.add_done_callback(functools.partial(self._on_task_done, job_id))
        self._update_positions()

    def _on_task_done(self, job_id: str, task: asyncio.Task) -> None:
        if self._running.get(job_id) is task:
            del self._running[job_id]
        job = self.store.get(job_id)
        if job is not None and job.status == JobStatus.RUNNING:
            # The runner never reached its own handlers (e.g. cancelled before it started).
            if self._stopping:
                job.status = JobStatus.QUEUED
                job.stage_detail = "interrupted by shutdown; will resume"
                self.store.save(job, force=True)
            elif task.cancelled():
                self._finish(job, JobStatus.CANCELLED, JobError(code="cancelled", message="Cancelled by request", retryable=True))
            else:
                exc = task.exception()
                self._finish(job, JobStatus.FAILED, JobError(code="internal_error", message=f"{type(exc).__name__}: {exc}", retryable=True))
        self._kick()

    def _finish(self, job: Job, status: JobStatus, error: JobError | None = None) -> None:
        job.status = status
        job.error = error
        job.queue_position = None
        if status in TERMINAL_STATUSES:
            job.finished_at = utcnow()
            if status == JobStatus.COMPLETED:
                job.progress = 100
                job.stage = "done"
                job.stage_detail = None
        self.store.save(job, force=True)

    async def _run(self, job: Job) -> None:
        job.status = JobStatus.RUNNING
        job.started_at = job.started_at or utcnow()
        job.queue_position = None
        job.attempts += 1
        job.error = None
        if job.stage == "queued":
            job.stage = "starting"
            job.stage_detail = None
        self.store.save(job, force=True)
        ctx = JobContext(job, self.store, self.limits, self.settings, self.services)
        try:
            result = await self.runners[job.kind](ctx)
            job.result = result
            self._finish(job, JobStatus.COMPLETED)
        except asyncio.CancelledError:
            if self._stopping:
                job.status = JobStatus.QUEUED  # resumed by start() on next boot
                job.stage_detail = "interrupted by shutdown; will resume"
                self.store.save(job, force=True)
            else:
                self._finish(job, JobStatus.CANCELLED, JobError(code="cancelled", message="Cancelled by request", retryable=True))
        except JobFailed as exc:
            ctx.log(f"FAILED {exc.code}: {exc.message}")
            self._finish(job, JobStatus.FAILED, JobError(code=exc.code, message=exc.message, retryable=exc.retryable))
        except Exception as exc:  # unexpected bug or provider outage
            ctx.log_exception(exc)
            log.exception("job %s crashed", job.id)
            self._finish(
                job,
                JobStatus.FAILED,
                JobError(code="internal_error", message=f"{type(exc).__name__}: {str(exc)[:500]}", retryable=True),
            )

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.store.flush()

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                self.cleanup_expired()
            except Exception:
                log.exception("job cleanup failed")
            await asyncio.sleep(900)

    def cleanup_expired(self) -> int:
        cutoff = utcnow() - timedelta(hours=self.settings.job_ttl_hours)
        removed = 0
        for job in self.store.all():
            if job.is_terminal and job.finished_at and job.finished_at < cutoff:
                shutil.rmtree(self.settings.projects_dir / job.id, ignore_errors=True)
                self.store.delete(job.id)
                removed += 1
        return removed
