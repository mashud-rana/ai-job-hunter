from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RemoteType = Literal["remote", "hybrid", "onsite", "unknown"]


class RawJob(BaseModel):
    """Standardized job structure every collector returns."""

    source: str
    external_id: str
    title: str
    company: str | None = None
    location: str | None = None
    job_type: str | None = None
    description: str = ""
    salary: str | None = "Not specified"
    apply_url: str
    posted_at: datetime | None = None
    collected_at: datetime | None = None
    expires_at: datetime | None = None
    tags: list[str] = Field(default_factory=list)
    # Structured hints a source may provide directly (overrides inference).
    remote_hint: RemoteType | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    raw: dict[str, Any] | None = None


class NormalizedJob(BaseModel):
    source: str
    external_id: str
    title: str
    company: str | None
    location: str | None
    country: str | None
    remote_type: RemoteType
    job_type: str | None
    description: str
    salary_text: str | None
    salary_min: float | None
    salary_max: float | None
    currency: str | None
    apply_url: str
    canonical_url: str
    content_hash: str
    tags: list[str]
    posted_at: datetime | None
    collected_at: datetime
    expires_at: datetime | None
    raw: dict[str, Any] | None = None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_key: str | None = None
    external_id: str
    title: str
    company: str | None
    location: str | None
    country: str | None
    remote_type: str
    job_type: str | None
    salary_text: str | None
    salary_min: float | None
    salary_max: float | None
    currency: str | None
    apply_url: str
    tags: list[str]
    posted_at: datetime | None
    collected_at: datetime
    is_expired: bool
    match_score: int | None = None
    match_category: str | None = None


class JobDetailOut(JobOut):
    description: str
    match: MatchOut | None = None


class MatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: int
    match_score: int
    category: str
    matched_skills: list[str]
    missing_skills: list[str]
    experience_match: bool | None
    location_eligible: bool | None
    confidence: float
    ai_reason: str
    recommendation: str
    provider: str
    model: str | None
    created_at: datetime


class MatchWithJobOut(MatchOut):
    job: JobOut


JobDetailOut.model_rebuild()
