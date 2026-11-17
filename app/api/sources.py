from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.api.deps import ContainerDep, require_api_key
from app.api.serializers import source_out
from app.database.repositories import SourceRepository
from app.logging import get_logger
from app.schemas.common import SourceOut

log = get_logger(__name__)
router = APIRouter(prefix="/api", tags=["sources"], dependencies=[Depends(require_api_key)])


class SourceUpdate(BaseModel):
    enabled: bool


@router.get("/sources", response_model=list[SourceOut])
async def list_sources(c: ContainerDep) -> list[SourceOut]:
    async with c.db.session() as session:
        return [source_out(s) for s in await SourceRepository(session).list_all()]


@router.patch("/sources/{key}", response_model=SourceOut)
async def update_source(key: str, data: SourceUpdate, c: ContainerDep) -> SourceOut:
    async with c.db.session() as session:
        source = await SourceRepository(session).set_enabled(key, data.enabled)
        if source is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found")
        return source_out(source)


_background: set[asyncio.Task[Any]] = set()


@router.post("/tasks/collect", status_code=status.HTTP_202_ACCEPTED)
async def trigger_collection(c: ContainerDep, force: bool = False) -> dict[str, str]:
    """Start a collect+match run now (in the background)."""
    from app.scheduler.tasks import collect_and_match

    if any(not t.done() for t in _background):
        raise HTTPException(status.HTTP_409_CONFLICT, "a manual run is already in progress")

    async def run() -> None:
        try:
            await collect_and_match(c, force=force)
        except Exception:
            log.exception("manual_collection_failed")

    task = asyncio.create_task(run())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"status": "started"}
