from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.database.repositories import (
    ApplicationRepository,
    JobRepository,
    LogRepository,
    MatchRepository,
    ProfileRepository,
)
from app.database.store import JsonStore


class StatsService:
    def __init__(self, db: JsonStore) -> None:
        self.db = db

    async def overview(self, hours: int = 24) -> dict[str, Any]:
        since = datetime.now(UTC) - timedelta(hours=hours)
        async with self.db.session() as session:
            jobs = await JobRepository(session).stats(since=since)
            ref = await ProfileRepository(session).active_ref()
            matches = await MatchRepository(session).score_distribution(ref) if ref else {}
            applications = await ApplicationRepository(session).counts()
            collection = await LogRepository(session).collection_summary(since)
        return {
            "window_hours": hours,
            "jobs": jobs,
            "matches_by_category": matches,
            "applications_by_status": applications,
            "collection": collection,
        }
