"""Job collection engine: run collectors concurrently, normalize, dedupe, persist, log."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.collectors.base import BaseCollector, is_relevant
from app.config import Settings
from app.database.models import CollectionLog
from app.database.repositories import JobRepository, LogRepository, SourceRepository
from app.database.repositories.jobs import InvalidRowError
from app.database.store import JsonStore
from app.logging import get_logger
from app.schemas.job import RawJob
from app.utils.normalize import InvalidJobError, is_expired, normalize_job
from app.utils.text import truncate

log = get_logger(__name__)


@dataclass
class SourceResult:
    source: str
    status: str  # success | failed | skipped
    fetched: int = 0
    new: int = 0
    duplicates: int = 0
    invalid: int = 0
    irrelevant: int = 0
    error: str | None = None
    new_job_ids: list[int] = field(default_factory=list)


@dataclass
class CollectionSummary:
    results: list[SourceResult]

    @property
    def new_job_ids(self) -> list[int]:
        return [i for r in self.results for i in r.new_job_ids]

    @property
    def failed(self) -> list[SourceResult]:
        return [r for r in self.results if r.status == "failed"]

    def totals(self) -> dict[str, int]:
        return {
            k: sum(getattr(r, k) for r in self.results)
            for k in ("fetched", "new", "duplicates", "invalid", "irrelevant")
        }


class CollectionService:
    def __init__(self, db: JsonStore, settings: Settings, collectors: list[BaseCollector]) -> None:
        self.db = db
        self.settings = settings
        self.collectors = collectors

    async def sync_sources(self) -> None:
        """Register every collector in job_sources with its current availability."""
        async with self.db.session() as session:
            repo = SourceRepository(session)
            for c in self.collectors:
                av = c.availability()
                await repo.upsert(c.key, c.name, av.available, av.reason)

    async def run(self, only: list[str] | None = None, force: bool = False) -> CollectionSummary:
        await self.sync_sources()
        sem = asyncio.Semaphore(max(1, self.settings.collector_concurrency))
        targets = [c for c in self.collectors if not only or c.key in only]

        async def guarded(c: BaseCollector) -> SourceResult:
            async with sem:
                try:
                    return await self.run_one(c, force=force)
                except Exception as exc:  # last line of defence: never let one source kill the run
                    log.exception("collector_crashed", source=c.key)
                    return SourceResult(source=c.key, status="failed", error=str(exc))

        results = await asyncio.gather(*(guarded(c) for c in targets))
        summary = CollectionSummary(list(results))
        log.info("collection_finished", **summary.totals(), failed=[r.source for r in summary.failed])
        return summary

    async def run_one(self, collector: BaseCollector, force: bool = False) -> SourceResult:
        started = datetime.now(UTC)
        async with self.db.session() as session:
            source = await SourceRepository(session).get_by_key(collector.key)
            assert source is not None
            source_id = source.id
            skip_reason = None
            if not source.available:
                skip_reason = source.unavailable_reason or "unavailable"
            elif not source.enabled:
                skip_reason = "disabled"
            elif (
                not force
                and collector.min_interval_hours
                and source.last_success_at
                and source.last_success_at > started - timedelta(hours=collector.min_interval_hours)
            ):
                skip_reason = f"ran less than {collector.min_interval_hours}h ago"
        if skip_reason:
            log.info("collector_skipped", source=collector.key, reason=skip_reason)
            return SourceResult(source=collector.key, status="skipped", error=skip_reason)

        result = SourceResult(source=collector.key, status="success")
        raws: list[RawJob] = []
        try:
            raws = await asyncio.wait_for(collector.collect(), timeout=self.settings.collector_timeout)
        except Exception as exc:
            result.status = "failed"
            result.error = truncate(f"{type(exc).__name__}: {exc}", 1000)
            log.warning("collector_failed", source=collector.key, error=result.error)

        result.fetched = len(raws)
        if raws:
            await self._persist(collector, source_id, raws, result)

        async with self.db.session() as session:
            await LogRepository(session).add_collection(
                CollectionLog(
                    source_id=source_id,
                    started_at=started,
                    finished_at=datetime.now(UTC),
                    status=result.status,
                    fetched=result.fetched,
                    new=result.new,
                    duplicates=result.duplicates,
                    invalid=result.invalid,
                    error=result.error,
                )
            )
            src = await SourceRepository(session).get_by_key(collector.key)
            if src:
                await SourceRepository(session).touch(src, started, result.status == "success")
        log.info(
            "collector_done",
            source=collector.key,
            status=result.status,
            fetched=result.fetched,
            new=result.new,
            duplicates=result.duplicates,
            invalid=result.invalid,
            irrelevant=result.irrelevant,
        )
        return result

    async def _persist(
        self, collector: BaseCollector, source_id: int, raws: list[RawJob], result: SourceResult
    ) -> None:
        keywords: list[str] = []
        if self.settings.relevance_filter:
            keywords = collector.relevance_keywords or self.settings.search_keywords
        now = datetime.now(UTC)
        async with self.db.session() as session:
            repo = JobRepository(session)
            for raw in raws:
                if not collector.targeted and not is_relevant(raw, keywords):
                    result.irrelevant += 1
                    continue
                try:
                    job = normalize_job(raw, now)
                except (InvalidJobError, ValueError) as exc:
                    result.invalid += 1
                    log.debug("job_invalid", source=collector.key, reason=str(exc))
                    continue
                if is_expired(job.posted_at, job.expires_at, self.settings.job_max_age_days, now):
                    result.invalid += 1
                    continue
                try:
                    entity = await repo.insert_if_new(job, source_id)
                except InvalidRowError as exc:
                    result.invalid += 1
                    log.warning("job_rejected_by_db", source=collector.key, error=str(exc))
                    continue
                if entity is None:
                    result.duplicates += 1
                else:
                    result.new += 1
                    result.new_job_ids.append(entity.id)
