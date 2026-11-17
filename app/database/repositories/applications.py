from __future__ import annotations

from typing import Any

from app.database.models import JobApplication, utcnow
from app.database.repositories.base import Page, paginate
from app.database.store import JsonStore


class ApplicationRepository:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    async def get(self, app_id: int) -> JobApplication | None:
        return self.store.get("applications", app_id)

    async def get_by_job(self, job_id: int) -> JobApplication | None:
        return next((a for a in self.store.all("applications") if a.job_id == job_id), None)

    async def create(self, data: dict[str, Any]) -> JobApplication:
        entity = JobApplication(**data)
        entity.job = self.store.get("jobs", entity.job_id)
        return self.store.add("applications", entity)

    async def update(self, entity: JobApplication, data: dict[str, Any]) -> JobApplication:
        for key, value in data.items():
            setattr(entity, key, value)
        entity.updated_at = utcnow()
        self.store.touch("applications")
        return entity

    async def list_applications(
        self, status: list[str] | None = None, page: int = 1, per_page: int = 20
    ) -> Page[JobApplication]:
        rows = [a for a in self.store.all("applications") if not status or a.status in status]
        rows.sort(key=lambda a: (a.updated_at, a.id), reverse=True)
        return paginate(rows, page, per_page)

    async def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.store.all("applications"):
            out[a.status] = out.get(a.status, 0) + 1
        return out
