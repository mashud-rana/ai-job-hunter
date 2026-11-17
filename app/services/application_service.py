"""Job application lifecycle tracking. Nothing here submits applications anywhere."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.database.models import JobApplication
from app.database.repositories import ApplicationRepository, JobRepository, Page
from app.database.store import JsonStore
from app.schemas.application import ApplicationCreate, ApplicationUpdate


class ApplicationNotFound(LookupError):
    pass


class ApplicationService:
    def __init__(self, db: JsonStore) -> None:
        self.db = db

    async def create(self, data: ApplicationCreate) -> JobApplication:
        payload: dict[str, Any] = data.model_dump(exclude_none=True)
        if "job_url" in payload:
            payload["job_url"] = str(payload["job_url"])
        async with self.db.session() as session:
            if data.job_id is not None:
                job = await JobRepository(session).get(data.job_id)
                if job is None:
                    raise ApplicationNotFound("job not found")
                existing = await ApplicationRepository(session).get_by_job(job.id)
                if existing:  # one application per job: update instead of duplicating
                    return await self._apply_update(session, existing, payload)
                payload.setdefault("company", job.company)
                payload.setdefault("position", job.title)
                payload.setdefault("job_url", job.apply_url)
            if payload.get("status") == "applied":
                payload.setdefault("applied_at", datetime.now(UTC))
            return await ApplicationRepository(session).create(payload)

    async def set_status_for_job(self, job_id: int, status: str) -> JobApplication:
        return await self.create(ApplicationCreate(job_id=job_id, status=status))  # type: ignore[arg-type]

    async def update(self, app_id: int, data: ApplicationUpdate) -> JobApplication:
        payload = data.model_dump(exclude_unset=True)
        if payload.get("job_url") is not None:
            payload["job_url"] = str(payload["job_url"])
        async with self.db.session() as session:
            entity = await ApplicationRepository(session).get(app_id)
            if entity is None:
                raise ApplicationNotFound("application not found")
            return await self._apply_update(session, entity, payload)

    @staticmethod
    async def _apply_update(session, entity: JobApplication, payload: dict[str, Any]) -> JobApplication:
        payload = {k: v for k, v in payload.items() if k != "job_id"}
        if payload.get("status") == "applied" and entity.applied_at is None and "applied_at" not in payload:
            payload["applied_at"] = datetime.now(UTC)
        return await ApplicationRepository(session).update(entity, payload)

    async def list_applications(
        self, status: list[str] | None = None, page: int = 1, per_page: int = 20
    ) -> Page[JobApplication]:
        async with self.db.session() as session:
            return await ApplicationRepository(session).list_applications(status, page, per_page)

    async def counts(self) -> dict[str, int]:
        async with self.db.session() as session:
            return await ApplicationRepository(session).counts()
