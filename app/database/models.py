"""Domain records stored as JSON (no database).

Plain dataclasses with the same attribute names the services, API, bot and templates use.
Relationship attributes (``Job.source``, ``JobMatch.job``, ``JobApplication.job``) are object
references resolved by the store at load time and are never serialized.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any, ClassVar

APPLICATION_STATUSES = ("saved", "interested", "applied", "interview", "rejected", "offer")
REMOTE_TYPES = ("remote", "hybrid", "onsite", "unknown")
DOCUMENT_KINDS = ("cover_letter", "email", "linkedin_message")
RECOMMENDATIONS = ("apply", "consider", "skip")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _dump(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [_dump(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    return value


def _parse_dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(eq=False)
class Record:
    """Base: id + timestamps, JSON (de)serialization."""

    DATETIME_FIELDS: ClassVar[tuple[str, ...]] = ()
    REFERENCES: ClassVar[tuple[str, ...]] = ()  # relationship attributes, not serialized

    def to_dict(self) -> dict[str, Any]:
        return {f.name: _dump(getattr(self, f.name)) for f in fields(self) if f.name not in self.REFERENCES}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Any:
        names = {f.name for f in fields(cls)} - set(cls.REFERENCES)
        kwargs = {k: v for k, v in data.items() if k in names}
        for name in cls.DATETIME_FIELDS:
            if name in kwargs:
                kwargs[name] = _parse_dt(kwargs[name])
        return cls(**kwargs)


@dataclass(eq=False)
class JobSource(Record):
    DATETIME_FIELDS: ClassVar = ("last_run_at", "last_success_at", "created_at", "updated_at")

    key: str = ""
    name: str = ""
    id: int | None = None
    enabled: bool = True
    available: bool = True
    unavailable_reason: str | None = None
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(eq=False)
class Job(Record):
    DATETIME_FIELDS: ClassVar = ("posted_at", "collected_at", "expires_at", "created_at", "updated_at")
    REFERENCES: ClassVar = ("source",)

    source_id: int = 0
    external_id: str = ""
    title: str = ""
    apply_url: str = ""
    canonical_url: str = ""
    content_hash: str = ""
    id: int | None = None
    company: str | None = None
    location: str | None = None
    country: str | None = None
    remote_type: str = "unknown"
    job_type: str | None = None
    description: str = ""
    salary_text: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    tags: list[str] = field(default_factory=list)
    posted_at: datetime | None = None
    collected_at: datetime = field(default_factory=utcnow)
    expires_at: datetime | None = None
    is_expired: bool = False
    raw: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    source: JobSource | None = None


@dataclass(eq=False)
class CandidateSkill(Record):
    name: str = ""
    normalized_name: str = ""
    id: int | None = None
    profile_id: int | None = None
    category: str | None = None
    years: float | None = None
    level: str | None = None


@dataclass(eq=False)
class CandidateProfile(Record):
    DATETIME_FIELDS: ClassVar = ("created_at", "updated_at")

    name: str = ""
    id: int | None = None
    headline: str | None = None
    years_experience: float = 0
    target_titles: list[str] = field(default_factory=list)
    preferred_locations: list[str] = field(default_factory=list)
    job_types: list[str] = field(default_factory=list)
    min_salary: float | None = None
    salary_currency: str | None = None
    summary: str | None = None
    cv_text: str | None = None
    cv_filename: str | None = None
    is_verified: bool = False
    is_active: bool = True
    # Bumped on every change; matches are keyed by version so stale ones get recomputed.
    version: int = 1
    # Profile extracted from an uploaded CV, awaiting explicit confirmation.
    pending_profile: dict[str, Any] | None = None
    skills: list[CandidateSkill] = field(default_factory=list)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["skills"] = [s.to_dict() for s in self.skills]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CandidateProfile:
        skills = [CandidateSkill.from_dict(s) for s in data.get("skills") or []]
        profile = super().from_dict({k: v for k, v in data.items() if k != "skills"})
        profile.skills = skills
        return profile


@dataclass(eq=False)
class JobMatch(Record):
    DATETIME_FIELDS: ClassVar = ("notified_at", "created_at", "updated_at")
    REFERENCES: ClassVar = ("job", "profile")

    job_id: int = 0
    profile_id: int = 0
    profile_version: int = 1
    match_score: int = 0
    category: str = "poor"
    recommendation: str = "skip"
    provider: str = "rules"
    id: int | None = None
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    experience_match: bool | None = None
    location_eligible: bool | None = None
    confidence: float = 0.0
    ai_reason: str = ""
    model: str | None = None
    notified_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    job: Job | None = None
    profile: CandidateProfile | None = None


@dataclass(eq=False)
class JobApplication(Record):
    DATETIME_FIELDS: ClassVar = ("applied_at", "follow_up_at", "created_at", "updated_at")
    REFERENCES: ClassVar = ("job",)

    position: str = ""
    id: int | None = None
    job_id: int | None = None
    status: str = "saved"
    company: str | None = None
    job_url: str | None = None
    applied_at: datetime | None = None
    follow_up_at: datetime | None = None
    notes: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    job: Job | None = None


@dataclass(eq=False)
class GeneratedDocument(Record):
    DATETIME_FIELDS: ClassVar = ("created_at", "updated_at")

    kind: str = ""
    content: str = ""
    id: int | None = None
    job_id: int | None = None
    profile_id: int | None = None
    provider: str | None = None
    model: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)


@dataclass(eq=False)
class NotificationLog(Record):
    DATETIME_FIELDS: ClassVar = ("sent_at",)

    kind: str = ""
    status: str = ""
    id: int | None = None
    channel: str = "telegram"
    job_id: int | None = None
    chat_id: str | None = None
    error: str | None = None
    sent_at: datetime = field(default_factory=utcnow)


@dataclass(eq=False)
class CollectionLog(Record):
    DATETIME_FIELDS: ClassVar = ("started_at", "finished_at")
    REFERENCES: ClassVar = ("source",)

    source_id: int = 0
    status: str = ""
    id: int | None = None
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    invalid: int = 0
    error: str | None = None
    source: JobSource | None = None
