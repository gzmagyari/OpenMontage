"""Job data model (persisted as JSON, returned by the polling endpoint)."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


class JobStatus(str, Enum):
    DRAFT = "draft"            # created but not started: prompt/references still editable
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}


class JobKind(str, Enum):
    VIDEO = "video"            # prompt + references -> finished video (agent-driven pipeline run)
    REVISE = "revise"          # previous job + feedback -> revised video (resumes the project)
    TOOL_RUN = "tool_run"      # raw registry tool execution (internal API)


class JobError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class CostLine(BaseModel):
    item: str
    usd: float
    provider: str | None = None
    estimated: bool = False


class JobCost(BaseModel):
    estimated_usd: float | None = None
    spent_usd: float = 0.0
    lines: list[CostLine] = Field(default_factory=list)


class Job(BaseModel):
    id: str
    kind: JobKind
    status: JobStatus = JobStatus.QUEUED
    stage: str | None = None
    stage_detail: str | None = None
    progress: int = 0
    queue_position: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    idempotency_key: str | None = None
    parent_job_id: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    cost: JobCost = Field(default_factory=JobCost)
    error: JobError | None = None
    result: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    attempts: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def public(self, files_base_url: str | None = None) -> dict[str, Any]:
        """Polling representation. File references become absolute URLs when a base is given."""
        data = self.model_dump(mode="json", exclude={"request", "idempotency_key"})
        if self.status == JobStatus.DRAFT:
            # A draft is still being assembled: the client needs to see what is attached so far.
            data["request"] = self.request
        data["cost"] = {
            "estimated_usd": self.cost.estimated_usd,
            "spent_usd": round(self.cost.spent_usd, 4),
            "lines": [line.model_dump(mode="json") for line in self.cost.lines],
        }
        result = data.get("result")
        if result and files_base_url and isinstance(result.get("files"), dict):
            base = files_base_url.rstrip("/")
            result["files"] = {
                key: {**value, "url": f"{base}/v1/jobs/{self.id}/files/{value['name']}"}
                for key, value in result["files"].items()
            }
        return data
