"""ORM → API schema conversion helpers."""

from __future__ import annotations

from app.database.models import GeneratedDocument, Job, JobMatch, JobSource
from app.schemas.common import DocumentOut, SourceOut
from app.schemas.job import JobDetailOut, JobOut, MatchOut


def job_out(job: Job, match: JobMatch | None = None) -> JobOut:
    return JobOut.model_validate(
        {
            **{k: getattr(job, k) for k in JobOut.model_fields if hasattr(job, k)},
            "source_key": job.source.key if job.source is not None else None,
            "match_score": match.match_score if match else None,
            "match_category": match.category if match else None,
        }
    )


def job_detail_out(job: Job, match: JobMatch | None) -> JobDetailOut:
    base = job_out(job, match).model_dump()
    return JobDetailOut(**base, description=job.description, match=MatchOut.model_validate(match) if match else None)


def source_out(s: JobSource) -> SourceOut:
    return SourceOut(
        key=s.key,
        name=s.name,
        enabled=s.enabled,
        available=s.available,
        unavailable_reason=s.unavailable_reason,
        last_run_at=s.last_run_at.isoformat() if s.last_run_at else None,
        last_success_at=s.last_success_at.isoformat() if s.last_success_at else None,
    )


def document_out(d: GeneratedDocument) -> DocumentOut:
    return DocumentOut(
        id=d.id,
        job_id=d.job_id,
        kind=d.kind,
        content=d.content,
        provider=d.provider,
        model=d.model,
        created_at=d.created_at.isoformat(),
    )
