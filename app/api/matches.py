from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import ContainerDep, PaginationDep, page_meta, require_api_key
from app.api.serializers import job_out
from app.database.repositories import MatchRepository, ProfileRepository
from app.schemas.common import Paginated
from app.schemas.job import MatchOut, MatchWithJobOut

router = APIRouter(prefix="/api/matches", tags=["matches"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=Paginated[MatchWithJobOut])
async def list_matches(
    c: ContainerDep,
    pagination: PaginationDep,
    min_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    sort: Literal["score", "recent"] = "score",
    include_expired: bool = False,
) -> dict[str, Any]:
    async with c.db.session() as session:
        ref = await ProfileRepository(session).active_ref()
        if ref is None:
            return {"items": [], "total": 0, "page": 1, "per_page": pagination.per_page, "pages": 1}
        page = await MatchRepository(session).list_matches(
            ref, min_score, pagination.page, pagination.per_page, sort=sort, include_expired=include_expired
        )
        items = [MatchWithJobOut(**MatchOut.model_validate(m).model_dump(), job=job_out(m.job, m)) for m in page.items]
    return {"items": items, **page_meta(page)}
