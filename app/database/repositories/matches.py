from __future__ import annotations

from datetime import datetime

from app.database.models import JobMatch, utcnow
from app.database.repositories.base import Page, paginate
from app.database.repositories.jobs import ProfileRef
from app.database.store import JsonStore


class MatchRepository:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    async def save(self, match: JobMatch) -> JobMatch | None:
        """One match per (job, profile, profile version); returns None if it already exists."""
        for m in self.store.all("matches"):
            if (m.job_id, m.profile_id, m.profile_version) == (match.job_id, match.profile_id, match.profile_version):
                return None
        match.job = self.store.get("jobs", match.job_id)
        if match.job is None:
            return None  # job was pruned meanwhile
        return self.store.add("matches", match)

    def _base(self, profile: ProfileRef, min_score: int | None, include_expired: bool) -> list[JobMatch]:
        out = []
        for m in self.store.all("matches"):
            if m.profile_id != profile.id or m.profile_version != profile.version or m.job is None:
                continue
            if not include_expired and m.job.is_expired:
                continue
            if min_score is not None and m.match_score < min_score:
                continue
            out.append(m)
        return out

    async def list_matches(
        self,
        profile: ProfileRef,
        min_score: int | None = None,
        page: int = 1,
        per_page: int = 20,
        sort: str = "score",
        include_expired: bool = False,
    ) -> Page[JobMatch]:
        rows = self._base(profile, min_score, include_expired)
        if sort == "recent":
            rows.sort(key=lambda m: (m.created_at, m.id), reverse=True)
        else:
            rows.sort(key=lambda m: (m.match_score, m.id), reverse=True)
        return paginate(rows, page, per_page)

    async def recent_above(
        self, profile: ProfileRef, min_score: int, since: datetime, limit: int = 10
    ) -> list[JobMatch]:
        rows = [m for m in self._base(profile, min_score, False) if m.created_at >= since]
        rows.sort(key=lambda m: m.match_score, reverse=True)
        return rows[:limit]

    async def pending_alerts(self, profile: ProfileRef, threshold: int, limit: int = 20) -> list[JobMatch]:
        rows = [m for m in self._base(profile, threshold, False) if m.notified_at is None]
        rows.sort(key=lambda m: m.match_score, reverse=True)
        return rows[:limit]

    async def mark_notified(self, match_id: int) -> None:
        m = self.store.get("matches", match_id)
        if m is not None:
            m.notified_at = utcnow()
            self.store.touch("matches")

    async def delete_for_profile(self, profile: ProfileRef) -> int:
        doomed = [
            m.id
            for m in self.store.all("matches")
            if m.profile_id == profile.id and m.profile_version == profile.version
        ]
        for mid in doomed:
            self.store.delete("matches", mid)
        return len(doomed)

    async def delete_stale_versions(self) -> int:
        """Drop matches for old profile versions (they're never shown again)."""
        current = {p.id: p.version for p in self.store.all("profiles")}
        doomed = [m.id for m in self.store.all("matches") if current.get(m.profile_id) != m.profile_version]
        for mid in doomed:
            self.store.delete("matches", mid)
        return len(doomed)

    async def score_distribution(self, profile: ProfileRef) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.store.all("matches"):
            if m.profile_id == profile.id and m.profile_version == profile.version:
                out[m.category] = out.get(m.category, 0) + 1
        return out
