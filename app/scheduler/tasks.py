"""Scheduled tasks. Each task is also callable directly (scripts, tests, API)."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.container import Container
from app.database.repositories import JobRepository, LogRepository, MatchRepository, SettingsRepository
from app.logging import get_logger
from app.utils.urls import resolves_to_public

log = get_logger(__name__)

LOG_RETENTION_DAYS = 60
KEEP_EXPIRED_DAYS = 14  # expired jobs are deleted from the JSON files after this
KEEP_CV_FILES = 5
URL_CHECK_SOURCES = ("careers", "arbeitnow", "remotive")  # sites where a HEAD check is polite and meaningful
URL_CHECK_LIMIT = 30


async def collect_and_match(
    container: Container, only: list[str] | None = None, force: bool = False, match: bool = True, notify: bool = True
) -> dict[str, Any]:
    """Collect → normalize → dedupe → store → AI match → alert."""
    started = time.monotonic()
    services = container.services
    summary = await services.collection.run(only=only, force=force)
    result: dict[str, Any] = {"collection": summary, "matching": None, "alerts": 0}
    if match:
        matching = await services.matching.run()
        result["matching"] = matching.as_dict()
    if notify:
        result["alerts"] = await services.notifications.send_high_match_alerts()
        if summary.failed:
            detail = "\n".join(f"{r.source}: {r.error}" for r in summary.failed)
            await services.notifications.send_error(f"{len(summary.failed)} source(s) failed", detail)
    log.info(
        "collect_and_match_done",
        seconds=round(time.monotonic() - started, 1),
        new_jobs=len(summary.new_job_ids),
        matching=result["matching"],
        alerts=result["alerts"],
    )
    await container.db.flush(force=True)  # make sure a remote (gist) store has everything
    return result


async def daily_digest(container: Container) -> bool:
    sent = await container.services.notifications.send_daily_digest()
    async with container.db.session() as session:
        await SettingsRepository(session).set(LAST_DIGEST_KEY, _local_today(container))
    await container.db.flush(force=True)
    log.info("daily_digest_done", sent=sent)
    return sent


async def nightly_maintenance(container: Container, check_urls: bool = True) -> dict[str, Any]:
    async with container.db.session() as session:
        jobs = JobRepository(session)
        expired = await jobs.mark_expired(container.settings.job_max_age_days)
        purged_logs = await LogRepository(session).purge_older_than(LOG_RETENTION_DAYS)
        stale_matches = await MatchRepository(session).delete_stale_versions()
        pruned_jobs = await jobs.prune(KEEP_EXPIRED_DAYS, container.settings.max_jobs)
        session.forget_old_seen()
        await SettingsRepository(session).set(LAST_MAINTENANCE_KEY, _local_today(container))
    dead_links = await _check_dead_links(container) if check_urls else 0
    removed_files = _clean_uploads(container)
    stats = await container.services.stats.overview(24)
    await container.db.flush(force=True)
    log.info(
        "nightly_maintenance_done",
        expired=expired,
        pruned_jobs=pruned_jobs,
        stale_matches=stale_matches,
        dead_links=dead_links,
        purged_logs=purged_logs,
        removed_files=removed_files,
        active_jobs=stats["jobs"]["active_jobs"],
    )
    return {
        "expired": expired,
        "pruned_jobs": pruned_jobs,
        "dead_links": dead_links,
        "purged_logs": purged_logs,
        "removed_files": removed_files,
        "stats": stats,
    }


async def _check_dead_links(container: Container) -> int:
    """HEAD-check a few of the oldest active postings; 404/410 means the listing was taken down."""
    async with container.db.session() as session:
        active = [
            j
            for j in session.all("jobs")
            if not j.is_expired and j.source is not None and j.source.key in URL_CHECK_SOURCES
        ]
        active.sort(key=lambda j: j.collected_at)
        candidates = [(j.id, j.apply_url) for j in active[:URL_CHECK_LIMIT]]
    dead = 0
    for job_id, url in candidates:
        if not await resolves_to_public(url):  # SSRF guard before the server fetches anything
            continue
        try:
            resp = await container.http.raw.head(url, timeout=10.0)
        except Exception:
            continue  # network trouble is not evidence the job is gone
        if resp.status_code in (404, 410):
            dead += 1
            async with container.db.session() as session:
                await JobRepository(session).set_expired(job_id)
    return dead


LAST_DIGEST_KEY = "last_digest_date"
LAST_MAINTENANCE_KEY = "last_maintenance_date"


def _local_now(container: Container) -> datetime:
    return datetime.now(ZoneInfo(container.settings.timezone))


def _local_today(container: Container) -> str:
    return _local_now(container).date().isoformat()


async def run_due_tasks(container: Container, force: bool = False) -> dict[str, Any]:
    """Everything a single external cron hit should do (no in-process scheduler needed):

    1. collect + match + alerts (each source still respects its own minimum interval),
    2. the daily digest, once per local day, at or after DIGEST_HOUR (Asia/Dhaka by default),
    3. maintenance, once per local day.
    """
    out: dict[str, Any] = {"digest": False, "maintenance": False}
    result = await collect_and_match(container, force=force)
    out["new_jobs"] = len(result["collection"].new_job_ids)
    out["matching"] = result["matching"]
    out["alerts"] = result["alerts"]
    today = _local_today(container)
    async with container.db.session() as session:
        settings_repo = SettingsRepository(session)
        last_digest = await settings_repo.get(LAST_DIGEST_KEY)
        last_maintenance = await settings_repo.get(LAST_MAINTENANCE_KEY)
    if last_maintenance != today:
        await nightly_maintenance(container)
        out["maintenance"] = True
    if last_digest != today and _local_now(container).hour >= container.settings.digest_hour:
        out["digest"] = await daily_digest(container)
    return out


def _clean_uploads(container: Container) -> int:
    folder = container.settings.uploads_dir
    if not folder.exists():
        return 0
    removed = 0
    files = sorted(folder.glob("cv_*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_CV_FILES:]:
        old.unlink(missing_ok=True)
        removed += 1
    cutoff = time.time() - 86400
    for tmp in folder.glob("*.tmp"):
        if tmp.stat().st_mtime < cutoff:
            tmp.unlink(missing_ok=True)
            removed += 1
    return removed


# ---------------------------------------------------------------------------------------------
# Entry points used by APScheduler. They take no arguments so they can be stored (by reference) in a
# persistent job store; the live container is registered at startup.

_runtime: dict[str, Any] = {"container": None, "tasks": set()}


def register_runtime(container: Container | None) -> None:
    _runtime["container"] = container


def _container() -> Container:
    c = _runtime["container"]
    if c is None:
        raise RuntimeError("scheduler runtime container not registered")
    return c


async def _guard(name: str, coro_factory, retry_in: timedelta | None = None) -> None:  # type: ignore[no-untyped-def]
    task = asyncio.current_task()
    if task is not None:
        _runtime["tasks"].add(task)
    try:
        await coro_factory()
    except Exception as exc:
        log.exception("scheduled_task_failed", task=name)
        container = _runtime["container"]
        if container is not None:
            try:
                await container.services.notifications.send_error(f"Scheduled task {name} failed", repr(exc))
            except Exception:
                log.exception("error_notification_failed")
            scheduler = container.extras.get("scheduler")
            if retry_in is not None and scheduler is not None and scheduler.running:
                run_at = datetime.now(UTC) + retry_in
                scheduler.add_job(
                    f"app.scheduler.tasks:{name}_job",
                    "date",
                    run_date=run_at,
                    id=f"{name}_retry",
                    replace_existing=True,
                    jobstore="memory",
                )
                log.info("scheduled_task_retry", task=name, run_at=run_at.isoformat())
    finally:
        if task is not None:
            _runtime["tasks"].discard(task)


async def collect_and_match_job() -> None:
    await _guard("collect_and_match", lambda: collect_and_match(_container()), retry_in=timedelta(minutes=20))


async def daily_digest_job() -> None:
    await _guard("daily_digest", lambda: daily_digest(_container()), retry_in=timedelta(minutes=10))


async def nightly_maintenance_job() -> None:
    await _guard("nightly_maintenance", lambda: nightly_maintenance(_container()))


async def wait_for_running(grace_seconds: float = 30.0) -> None:
    tasks = [t for t in _runtime["tasks"] if not t.done()]
    if tasks:
        log.info("waiting_for_scheduled_tasks", count=len(tasks))
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for t in pending:
            t.cancel()
