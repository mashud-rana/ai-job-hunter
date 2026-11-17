"""In-process APScheduler (optional; RUN_SCHEDULER). On hosts that sleep, use the /cron/sync URL instead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import Settings
from app.logging import get_logger

log = get_logger(__name__)

JOB_COLLECT = "collect_and_match"
JOB_DIGEST = "daily_digest"
JOB_MAINTENANCE = "nightly_maintenance"


def create_scheduler(settings: Settings) -> AsyncIOScheduler:
    # In-memory job store: schedules are code, and "what already ran today" lives in state.json.
    jobstores = {"default": MemoryJobStore(), "memory": MemoryJobStore()}
    return AsyncIOScheduler(
        jobstores=jobstores,
        timezone=settings.timezone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )


def desired_jobs(settings: Settings) -> dict[str, tuple[str, object]]:
    tz = settings.timezone
    return {
        JOB_COLLECT: (
            "app.scheduler.tasks:collect_and_match_job",
            IntervalTrigger(hours=settings.collect_interval_hours, timezone=tz),
        ),
        JOB_DIGEST: (
            "app.scheduler.tasks:daily_digest_job",
            CronTrigger(hour=settings.digest_hour, minute=0, timezone=tz),
        ),
        JOB_MAINTENANCE: (
            "app.scheduler.tasks:nightly_maintenance_job",
            CronTrigger(hour=0, minute=0, timezone=tz),
        ),
    }


def register_jobs(
    scheduler: AsyncIOScheduler, settings: Settings, first_collect_delay: timedelta | None = None
) -> None:
    """Idempotent registration. Must be called after scheduler.start(paused=True) so persisted jobs are loaded.

    Existing persisted jobs keep their next_run_time across restarts (restarts don't postpone the
    3-hourly collection); a job is only rescheduled when its trigger configuration changed.
    """
    first_collect_delay = timedelta(minutes=1) if first_collect_delay is None else first_collect_delay
    for job_id, (func, trigger) in desired_jobs(settings).items():
        existing = scheduler.get_job(job_id)
        if existing is None:
            kwargs = {}
            if job_id == JOB_COLLECT:
                kwargs["next_run_time"] = datetime.now(UTC) + first_collect_delay
            scheduler.add_job(func, trigger, id=job_id, name=job_id, replace_existing=True, **kwargs)
            log.info("scheduler_job_added", job=job_id, trigger=str(trigger))
        elif str(existing.trigger) != str(trigger) or existing.func_ref != func:
            scheduler.modify_job(job_id, func=func)
            scheduler.reschedule_job(job_id, trigger=trigger)
            log.info("scheduler_job_rescheduled", job=job_id, trigger=str(trigger))
