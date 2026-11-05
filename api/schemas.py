"""Request bodies for the public API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReferenceRole = Literal[
    "presenter", "person", "product", "logo", "style", "screenshot",
    "before", "after", "background", "voice", "music", "other",
]


class ReferenceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, description="Public http(s) URL to download")
    asset_id: str | None = Field(default=None, description="ID returned by POST /v1/assets")
    role: ReferenceRole | None = Field(default=None, description="How the reference should be used (auto-detected if omitted)")
    label: str | None = Field(default=None, max_length=300, description="Short hint, e.g. 'our app logo' or 'photo before editing'")

    @model_validator(mode="after")
    def _one_source(self) -> "ReferenceIn":
        if bool(self.url) == bool(self.asset_id):
            raise ValueError("each reference needs exactly one of 'url' or 'asset_id'")
        return self


class VideoOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_seconds: float = Field(default=30, ge=10, le=60)
    language: str = Field(default="en", min_length=2, max_length=8)
    max_budget_usd: float | None = Field(default=None, gt=0, le=100)
    captions: bool = True
    music: bool = True


class VideoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(..., min_length=3, max_length=4000)
    references: list[ReferenceIn] = Field(default_factory=list, max_length=12)
    options: VideoOptions = Field(default_factory=lambda: VideoOptions())
    idempotency_key: str | None = Field(default=None, max_length=200)


class ReviseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback: str = Field(..., min_length=3, max_length=2000)
    options: VideoOptions | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)


class ToolRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)
