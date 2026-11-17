from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.database.repositories import ProfileRepository, SourceRepository
from app.main import create_app
from app.scheduler import tasks
from app.scheduler.setup import JOB_COLLECT, JOB_DIGEST, JOB_MAINTENANCE, create_scheduler, register_jobs
from app.schemas.job import RawJob
from app.services.collection_service import CollectionService
from tests.test_collection_service import FakeCollector


def raw(i, **kw):
    data = dict(
        source="x",
        external_id=str(i),
        title=f"Senior Laravel Developer {i}",
        company=f"C{i}",
        location="Worldwide",
        apply_url=f"https://jobs.example.com/{i}",
        description="PHP Laravel MySQL Redis Docker, 4+ years",
    )
    data.update(kw)
    return RawJob(**data)


async def test_collect_and_match_end_to_end(container, sender):
    async with container.db.session() as s:
        await ProfileRepository(s).create(
            {
                "name": "T",
                "years_experience": 5,
                "target_titles": ["Senior Laravel Developer"],
                "preferred_locations": ["Bangladesh"],
            },
            [{"name": n} for n in ("PHP", "Laravel", "MySQL", "Redis", "Docker")],
            True,
        )
    good = FakeCollector(container.settings, "good", [raw(1), raw(2)])
    bad = FakeCollector(container.settings, "bad", exc=RuntimeError("source down"))
    container.services.collection = CollectionService(container.db, container.settings, [good, bad])

    result = await tasks.collect_and_match(container)
    assert result["collection"].totals()["new"] == 2
    assert result["matching"]["processed"] == 2 and result["alerts"] == 2
    texts = [m["text"] for m in sender.messages]
    assert sum("High match alert" in t for t in texts) == 2
    assert any("source(s) failed" in t and "source down" in t for t in texts)

    again = await tasks.collect_and_match(container)
    assert again["matching"]["processed"] == 0 and again["alerts"] == 0


async def test_daily_digest_task(container, sender):
    assert await tasks.daily_digest(container)
    assert "Daily job digest" in sender.messages[-1]["text"]


async def test_nightly_maintenance(container, tmp_path):
    async with container.db.session() as s:
        src = await SourceRepository(s).upsert("x", "X", True, None)
        from app.database.repositories import JobRepository
        from app.utils.normalize import normalize_job

        await JobRepository(s).insert_if_new(normalize_job(raw(1)), src.id)
        old = await JobRepository(s).insert_if_new(normalize_job(raw(2)), src.id)
        old.collected_at = datetime.now(UTC) - timedelta(days=100)
    uploads = container.settings.uploads_dir
    uploads.mkdir(parents=True)
    for i in range(7):
        p = uploads / f"cv_{i}.pdf"
        p.write_bytes(b"%PDF-")
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
    result = await tasks.nightly_maintenance(container, check_urls=False)
    assert result["expired"] == 1 and result["removed_files"] == 2
    assert result["pruned_jobs"] == 1  # expired and collected long ago -> removed from the JSON files
    assert sorted(p.name for p in uploads.iterdir()) == [f"cv_{i}.pdf" for i in range(2, 7)]
    async with container.db.session() as s:
        assert [j.is_expired for j in s.all("jobs")] == [False]
        assert s.was_seen("u:https://jobs.example.com/2")  # pruned job won't come back as "new"


async def test_dead_link_check_respects_ssrf_and_404(container, mock_http, monkeypatch):
    import httpx

    async with container.db.session() as s:
        src = await SourceRepository(s).upsert("careers", "Careers", True, None)
        from app.database.repositories import JobRepository
        from app.utils.normalize import normalize_job

        await JobRepository(s).insert_if_new(normalize_job(raw(1, apply_url="https://boards.example.com/1")), src.id)
        await JobRepository(s).insert_if_new(normalize_job(raw(2, apply_url="https://boards.example.com/2")), src.id)

    async def fake_resolves(url):
        return url.endswith("/1") or url.endswith("/2")

    monkeypatch.setattr(tasks, "resolves_to_public", fake_resolves)
    mock_http.head("https://boards.example.com/1").mock(return_value=httpx.Response(404))
    mock_http.head("https://boards.example.com/2").mock(return_value=httpx.Response(200))
    assert await tasks._check_dead_links(container) == 1


async def test_job_registration_and_timezone(settings):
    sched = create_scheduler(settings)
    sched.start(paused=True)
    try:
        register_jobs(sched, settings)
        jobs = {j.id: j for j in sched.get_jobs()}
        assert set(jobs) == {JOB_COLLECT, JOB_DIGEST, JOB_MAINTENANCE}
        assert isinstance(jobs[JOB_COLLECT].trigger, IntervalTrigger)
        assert jobs[JOB_COLLECT].trigger.interval == timedelta(hours=3)
        digest = jobs[JOB_DIGEST].trigger
        assert isinstance(digest, CronTrigger) and str(digest.timezone) == "Asia/Dhaka"
        assert "hour='9'" in str(digest) and "hour='0'" in str(jobs[JOB_MAINTENANCE].trigger)
        # first collection soon after start
        assert jobs[JOB_COLLECT].next_run_time <= datetime.now(UTC) + timedelta(minutes=2)
        # idempotent: re-registering keeps next_run_time
        before = jobs[JOB_COLLECT].next_run_time
        register_jobs(sched, settings)
        assert sched.get_job(JOB_COLLECT).next_run_time == before
        # changed interval -> rescheduled
        settings.collect_interval_hours = 6
        register_jobs(sched, settings)
        assert sched.get_job(JOB_COLLECT).trigger.interval == timedelta(hours=6)
    finally:
        sched.shutdown(wait=False)


async def test_guard_reports_failure_and_schedules_retry(container, sender):
    sched = create_scheduler(container.settings)
    sched.start(paused=True)
    container.extras["scheduler"] = sched
    tasks.register_runtime(container)
    try:

        async def boom():
            raise RuntimeError("kaput")

        await tasks._guard("collect_and_match", boom, retry_in=timedelta(minutes=20))
        assert any("kaput" in m["text"] for m in sender.messages)
        retry = sched.get_job("collect_and_match_retry")
        assert retry is not None and retry.next_run_time > datetime.now(UTC) + timedelta(minutes=19)
    finally:
        tasks.register_runtime(None)
        sched.shutdown(wait=False)


async def test_lifespan_starts_and_stops_scheduler(settings, container):
    import httpx

    settings.run_scheduler = True
    app = create_app(settings, container)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert container.extras["scheduler"].running
        body = (await c.get("/api/health")).json()
        assert body["checks"]["scheduler"] == "running"
    assert "scheduler" not in container.extras
    with pytest.raises(RuntimeError):
        tasks._container()
