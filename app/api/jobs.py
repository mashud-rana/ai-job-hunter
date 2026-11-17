from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.ai.cover_letter import DOCUMENT_KINDS, GenerationUnavailable
from app.api.deps import ContainerDep, PaginationDep, page_meta, require_api_key
from app.api.serializers import document_out, job_detail_out, job_out
from app.database.repositories import JobFilters, JobRepository, MatchRepository, ProfileRepository
from app.schemas.common import DocumentOut, Paginated
from app.schemas.job import JobDetailOut, JobOut
from app.services.document_service import NotFoundError

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_api_key)])

RemoteFilter = Literal["remote", "hybrid", "onsite", "unknown"]


def _split(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out[:10]


@router.get("", response_model=Paginated[JobOut])
async def list_jobs(
    c: ContainerDep,
    pagination: PaginationDep,
    q: Annotated[str | None, Query(max_length=200, description="search title/company/description")] = None,
    skills: Annotated[list[str] | None, Query(description="repeat or comma-separate; all must match")] = None,
    country: Annotated[str | None, Query(max_length=64)] = None,
    remote_type: RemoteFilter | None = None,
    min_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    source: Annotated[str | None, Query(max_length=64)] = None,
    sort: Literal["posted_at", "score", "collected_at"] = "posted_at",
    include_expired: bool = False,
) -> dict[str, Any]:
    filters = JobFilters(
        q=q,
        skills=_split(skills),
        country=country,
        remote_type=remote_type,
        source=source,
        min_score=min_score,
        include_expired=include_expired,
        sort=sort,
    )
    async with c.db.session() as session:
        ref = await ProfileRepository(session).active_ref()
        page = await JobRepository(session).list_jobs(filters, ref, pagination.page, pagination.per_page)
        items = [job_out(j, m) for j, m in page.items]
    return {"items": items, **page_meta(page)}


@router.get("/top", response_model=list[JobOut])
async def top_jobs(
    c: ContainerDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    min_score: Annotated[int, Query(ge=0, le=100)] = 70,
) -> list[JobOut]:
    async with c.db.session() as session:
        ref = await ProfileRepository(session).active_ref()
        if ref is None:
            return []
        page = await MatchRepository(session).list_matches(ref, min_score=min_score, page=1, per_page=limit)
        return [job_out(m.job, m) for m in page.items]


@router.get("/stats")
async def job_stats(c: ContainerDep, hours: Annotated[int, Query(ge=1, le=24 * 30)] = 24) -> dict[str, Any]:
    return await c.services.stats.overview(hours)


@router.get("/{job_id}", response_model=JobDetailOut)
async def get_job(job_id: int, c: ContainerDep) -> JobDetailOut:
    async with c.db.session() as session:
        repo = JobRepository(session)
        job = await repo.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
        match = await repo.get_match(job_id, await ProfileRepository(session).active_ref())
        return job_detail_out(job, match)


@router.post("/{job_id}/documents", response_model=DocumentOut, status_code=status.HTTP_201_CREATED)
async def generate_document(job_id: int, c: ContainerDep, kind: str = Query(...)) -> DocumentOut:
    """Generate (and store) a cover letter / email / LinkedIn message draft. Nothing is sent anywhere."""
    if kind not in DOCUMENT_KINDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"kind must be one of {list(DOCUMENT_KINDS)}")
    try:
        doc = await c.services.documents.generate(job_id, kind)
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except GenerationUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    return document_out(doc)


@router.get("/{job_id}/documents", response_model=list[DocumentOut])
async def list_documents(job_id: int, c: ContainerDep) -> list[DocumentOut]:
    return [document_out(d) for d in await c.services.documents.list_for_job(job_id)]
