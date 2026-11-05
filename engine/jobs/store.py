"""Disk-backed job store. One JSON file per job (atomic writes), cached in memory.

Single-process by design: the API runs one worker per container and the event
loop owns all mutations, so no cross-process locking is needed.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path

from engine.jobs.models import Job, JobKind, JobStatus, utcnow

log = logging.getLogger("openmontage.jobs")

_SAVE_INTERVAL_S = 0.5  # throttle for frequent progress updates


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader briefly holds the file
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


class JobStore:
    def __init__(self, jobs_dir: Path):
        self.jobs_dir = jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._idempotency: dict[str, str] = {}
        self._last_saved: dict[str, float] = {}
        self._dirty: set[str] = set()

    # -- paths -------------------------------------------------------------
    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def files_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "files"

    def _job_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    # -- persistence ---------------------------------------------------------
    def load_all(self) -> list[Job]:
        loaded: list[Job] = []
        for path in sorted(self.jobs_dir.glob("*/job.json")):
            try:
                job = Job.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:  # corrupt or foreign file: skip, never crash startup
                log.warning("Skipping unreadable job file %s: %s", path, exc)
                continue
            self._remember(job)
            loaded.append(job)
        return loaded

    def _remember(self, job: Job) -> None:
        self._jobs[job.id] = job
        if job.idempotency_key:
            self._idempotency[job.idempotency_key] = job.id

    def add(self, job: Job) -> None:
        self._remember(job)
        self.save(job, force=True)

    def save(self, job: Job, force: bool = False) -> None:
        job.updated_at = utcnow()
        now = time.monotonic()
        if not force and now - self._last_saved.get(job.id, 0.0) < _SAVE_INTERVAL_S:
            self._dirty.add(job.id)
            return
        self._write(job)

    def _write(self, job: Job) -> None:
        atomic_write_text(self._job_file(job.id), job.model_dump_json(indent=2))
        self._last_saved[job.id] = time.monotonic()
        self._dirty.discard(job.id)

    def flush(self) -> None:
        for job_id in list(self._dirty):
            job = self._jobs.get(job_id)
            if job is not None:
                self._write(job)

    # -- queries -------------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def find_idempotent(self, key: str) -> Job | None:
        job_id = self._idempotency.get(key)
        return self._jobs.get(job_id) if job_id else None

    def list(self, status: JobStatus | None = None, kind: JobKind | None = None, limit: int = 50) -> list[Job]:
        jobs = [
            j for j in self._jobs.values()
            if (status is None or j.status == status) and (kind is None or j.kind == kind)
        ]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[: max(1, limit)]

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def delete(self, job_id: str) -> None:
        job = self._jobs.pop(job_id, None)
        if job and job.idempotency_key:
            self._idempotency.pop(job.idempotency_key, None)
        self._last_saved.pop(job_id, None)
        self._dirty.discard(job_id)
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)
