from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

ApplicationStatus = Literal["saved", "interested", "applied", "interview", "rejected", "offer"]


class ApplicationCreate(BaseModel):
    job_id: int | None = None
    status: ApplicationStatus = "saved"
    company: str | None = Field(default=None, max_length=255)
    position: str | None = Field(default=None, max_length=500)
    job_url: HttpUrl | None = None
    applied_at: datetime | None = None
    follow_up_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=5000)

    @model_validator(mode="after")
    def _need_job_or_position(self) -> ApplicationCreate:
        if self.job_id is None and not self.position:
            raise ValueError("either job_id or position is required")
        return self


class ApplicationUpdate(BaseModel):
    status: ApplicationStatus | None = None
    company: str | None = Field(default=None, max_length=255)
    position: str | None = Field(default=None, min_length=1, max_length=500)
    job_url: HttpUrl | None = None
    applied_at: datetime | None = None
    follow_up_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=5000)


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int | None
    status: str
    company: str | None
    position: str
    job_url: str | None
    applied_at: datetime | None
    follow_up_at: datetime | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
