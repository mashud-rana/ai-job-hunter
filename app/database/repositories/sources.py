from __future__ import annotations

from datetime import datetime

from app.database.models import JobSource, utcnow
from app.database.store import JsonStore


class SourceRepository:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    async def get_by_key(self, key: str) -> JobSource | None:
        return next((s for s in self.store.all("sources") if s.key == key), None)

    async def get(self, source_id: int) -> JobSource | None:
        return self.store.get("sources", source_id)

    async def upsert(self, key: str, name: str, available: bool, reason: str | None) -> JobSource:
        source = await self.get_by_key(key)
        if source is None:
            return self.store.add(
                "sources", JobSource(key=key, name=name, available=available, unavailable_reason=reason)
            )
        if (source.name, source.available, source.unavailable_reason) != (name, available, reason):
            source.name, source.available, source.unavailable_reason = name, available, reason
            source.updated_at = utcnow()
            self.store.touch("sources")
        return source

    async def list_all(self) -> list[JobSource]:
        return sorted(self.store.all("sources"), key=lambda s: s.key)

    async def set_enabled(self, key: str, enabled: bool) -> JobSource | None:
        source = await self.get_by_key(key)
        if source:
            source.enabled = enabled
            source.updated_at = utcnow()
            self.store.touch("sources")
        return source

    async def touch(self, source: JobSource, at: datetime, success: bool) -> None:
        source.last_run_at = at
        if success:
            source.last_success_at = at
        self.store.touch("sources")
