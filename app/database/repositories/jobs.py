from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.database.models import Job, JobMatch
from app.database.repositories.base import Page, paginate
from app.database.store import JsonStore
from app.schemas.job import NormalizedJob
from app.utils.text import truncate

DUPLICATE_HASH_WINDOW_DAYS = 30
_FAR_PAST = datetime(1970, 1, 1, tzinfo=UTC)


class InvalidRowError(ValueError):
    pass


@dataclass
class JobFilters:
    q: str | None = None
    skills: list[str] = field(default_factory=list)
    country: str | None = None
    remote_type: str | None = None
    source: str | None = None
    min_score: int | None = None
    include_expired: bool = False
    sort: str = "posted_at"  # posted_at | score | collected_at


@dataclass
class ProfileRef:
    id: int
    version: int


def seen_keys(job: NormalizedJob | Job, source_id: int) -> tuple[str, str]:
    return f"x:{source_id}:{job.external_id}", f"u:{job.canonical_url}"


class JobRepository:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    # ---------- dedupe / insert ----------
    async def find_duplicate_id(self, job: NormalizedJob, source_id: int) -> int | None:
        cutoff = datetime.now(UTC) - timedelta(days=DUPLICATE_HASH_WINDOW_DAYS)
        for existing in self.store.all("jobs"):
            if (
                (existing.source_id == source_id and existing.external_id == job.external_id)
                or existing.canonical_url == job.canonical_url
                or (existing.content_hash == job.content_hash and existing.collected_at >= cutoff)
            ):
                return existing.id
        return None

    async def insert_if_new(self, job: NormalizedJob, source_id: int) -> Job | None:
        """Insert unless already stored, or stored before and pruned since (seen keys)."""
        keys = seen_keys(job, source_id)
        if self.store.was_seen(*keys) or await self.find_duplicate_id(job, source_id) is not None:
            return None
        data = job.model_dump(exclude={"source"})
        data["description"] = truncate(data["description"], self.store.settings.description_store_limit)
        entity = Job(source_id=source_id, **data)
        entity.source = self.store.get("sources", source_id)
        self.store.add("jobs", entity)
        self.store.remember(*keys)
        return entity

    # ---------- queries ----------
    def _match_index(self, profile: ProfileRef | None) -> dict[int, JobMatch]:
        if profile is None:
            return {}
        return {
            m.job_id: m
            for m in self.store.all("matches")
            if m.profile_id == profile.id and m.profile_version == profile.version
        }

    async def list_jobs(
        self, filters: JobFilters, profile: ProfileRef | None, page: int = 1, per_page: int = 20
    ) -> Page[tuple[Job, JobMatch | None]]:
        matches = self._match_index(profile)
        q = (filters.q or "").lower()
        skills = [s.lower() for s in filters.skills]
        country = (filters.country or "").lower()
        rows: list[tuple[Job, JobMatch | None]] = []
        for job in self.store.all("jobs"):
            if not filters.include_expired and job.is_expired:
                continue
            title, desc = job.title.lower(), (job.description or "").lower()
            if q and q not in title and q not in (job.company or "").lower() and q not in desc:
                continue
            if any(s not in title and s not in desc for s in skills):
                continue
            if country and (job.country or "").lower() != country:
                continue
            if filters.remote_type and job.remote_type != filters.remote_type:
                continue
            if filters.source and (job.source is None or job.source.key != filters.source):
                continue
            match = matches.get(job.id)
            if filters.min_score is not None and (match is None or match.match_score < filters.min_score):
                continue
            rows.append((job, match))

        if filters.sort == "score":
            rows.sort(key=lambda r: (r[1] is not None, r[1].match_score if r[1] else -1, r[0].id), reverse=True)
        elif filters.sort == "collected_at":
            rows.sort(key=lambda r: (r[0].collected_at, r[0].id), reverse=True)
        else:
            rows.sort(
                key=lambda r: (r[0].posted_at is not None, r[0].posted_at or _FAR_PAST, r[0].collected_at), reverse=True
            )
        return paginate(rows, page, per_page)

    async def get(self, job_id: int) -> Job | None:
        return self.store.get("jobs", job_id)

    async def get_match(self, job_id: int, profile: ProfileRef | None) -> JobMatch | None:
        return self._match_index(profile).get(job_id)

    async def unmatched_jobs(self, profile: ProfileRef, limit: int) -> list[Job]:
        """Active jobs without a match for the current profile version (avoids duplicate AI calls)."""
        matched = set(self._match_index(profile))
        jobs = [j for j in self.store.all("jobs") if not j.is_expired and j.id not in matched]
        jobs.sort(key=lambda j: (j.collected_at, j.id), reverse=True)
        return jobs[:limit]

    async def mark_expired(self, max_age_days: int, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=max_age_days)
        count = 0
        for job in self.store.all("jobs"):
            if job.is_expired:
                continue
            if (
                (job.expires_at and job.expires_at < now)
                or (job.posted_at and job.posted_at < cutoff)
                or (job.posted_at is None and job.collected_at < cutoff)
            ):
                job.is_expired = True
                count += 1
        if count:
            self.store.touch("jobs")
        return count

    async def set_expired(self, job_id: int) -> None:
        job = self.store.get("jobs", job_id)
        if job is not None and not job.is_expired:
            job.is_expired = True
            self.store.touch("jobs")

    async def prune(self, keep_expired_days: int, max_jobs: int) -> int:
        """Delete long-expired jobs (and their matches) and cap the total, keeping files small.

        Applications keep a copy of company/position/url, so tracked jobs survive as history.
        Pruned jobs stay in the seen-keys list, so they won't be re-imported as "new".
        """
        cutoff = datetime.now(UTC) - timedelta(days=keep_expired_days)
        tracked = {a.job_id for a in self.store.all("applications") if a.job_id}
        jobs = self.store.all("jobs")
        doomed = {j.id for j in jobs if j.is_expired and j.collected_at < cutoff and j.id not in tracked}
        survivors = sorted((j for j in jobs if j.id not in doomed), key=lambda j: (j.collected_at, j.id))
        overflow = len(survivors) - max_jobs
        for j in survivors:
            if overflow <= 0:
                break
            if j.id not in tracked:
                doomed.add(j.id)
                overflow -= 1
        for job_id in doomed:
            self.store.delete("jobs", job_id)
        for m in [m for m in self.store.all("matches") if m.job_id in doomed]:
            self.store.delete("matches", m.id)
        return len(doomed)

    async def stats(self, since: datetime | None = None) -> dict[str, Any]:
        jobs = self.store.all("jobs")
        by_source: dict[str, int] = {}
        by_remote: dict[str, int] = {}
        for j in jobs:
            key = j.source.key if j.source else "unknown"
            by_source[key] = by_source.get(key, 0) + 1
            if not j.is_expired:
                by_remote[j.remote_type] = by_remote.get(j.remote_type, 0) + 1
        out: dict[str, Any] = {
            "total_jobs": len(jobs),
            "active_jobs": sum(1 for j in jobs if not j.is_expired),
            "by_source": dict(sorted(by_source.items(), key=lambda kv: kv[1], reverse=True)),
            "by_remote_type": by_remote,
        }
        if since is not None:
            out["new_since"] = sum(1 for j in jobs if j.collected_at >= since)
        return out
