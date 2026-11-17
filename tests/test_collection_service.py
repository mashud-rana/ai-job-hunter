from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.collectors.base import Availability, BaseCollector
from app.schemas.job import RawJob
from app.services.collection_service import CollectionService


def raw(i: int, **kw) -> RawJob:
    data = dict(
        source="x",
        external_id=str(i),
        title=f"Senior Laravel Developer {i}",
        company=f"C{i}",
        location="Remote",
        apply_url=f"https://jobs.example.com/{i}",
        description="laravel php",
    )
    data.update(kw)
    return RawJob(**data)


class FakeCollector(BaseCollector):
    def __init__(self, settings, key, jobs=None, exc=None, targeted=True, available=True):
        super().__init__(settings, None)  # type: ignore[arg-type]
        self.key, self.name, self.targeted = key, key.title(), targeted
        self._jobs, self._exc, self._available = jobs or [], exc, available
        self.calls = 0

    def availability(self):
        return Availability.ok() if self._available else Availability.missing("no creds")

    async def collect(self):
        self.calls += 1
        if self._exc:
            raise self._exc
        return self._jobs


async def test_failure_isolation_dedupe_and_logs(settings, db):
    good = FakeCollector(settings, "good", [raw(1), raw(2), raw(2), raw(3, title="")])
    bad = FakeCollector(settings, "bad", exc=RuntimeError("boom"))
    off = FakeCollector(settings, "off", [raw(9)], available=False)
    broad = FakeCollector(
        settings, "broad", [raw(10, title="Marketing Manager", description="sales"), raw(11)], targeted=False
    )
    settings.search_keywords = ["laravel"]
    service = CollectionService(db, settings, [good, bad, off, broad])
    summary = await service.run()

    by = {r.source: r for r in summary.results}
    assert by["good"].status == "success" and by["good"].new == 2
    assert by["good"].duplicates == 1 and by["good"].invalid == 1
    assert by["bad"].status == "failed" and "boom" in by["bad"].error
    assert by["off"].status == "skipped" and off.calls == 0
    assert by["broad"].new == 1 and by["broad"].irrelevant == 1
    assert len(summary.new_job_ids) == 3

    async with db.session() as s:
        assert len(s.all("jobs")) == 3
        logs = s.all("collection_logs")
        assert {lg.status for lg in logs} == {"success", "failed"}  # skipped sources aren't logged
        off_src = next(x for x in s.all("sources") if x.key == "off")
        assert off_src.available is False and off_src.unavailable_reason == "no creds"

    # second run: everything is a duplicate
    summary2 = await service.run()
    assert {r.source: r.new for r in summary2.results}["good"] == 0


async def test_min_interval_and_expired(settings, db):
    old = raw(1, posted_at=datetime.now(UTC) - timedelta(days=100))
    c = FakeCollector(settings, "slow", [old, raw(2)])
    c.min_interval_hours = 6
    service = CollectionService(db, settings, [c])
    r1 = (await service.run()).results[0]
    assert r1.new == 1 and r1.invalid == 1  # expired listing filtered
    r2 = (await service.run()).results[0]
    assert r2.status == "skipped" and c.calls == 1
    r3 = (await service.run(force=True)).results[0]
    assert r3.status == "success" and c.calls == 2


async def test_timeout_is_isolated(settings, db):
    import asyncio

    class Slow(FakeCollector):
        async def collect(self):
            await asyncio.sleep(5)
            return []

    settings.collector_timeout = 0.05
    fast = FakeCollector(settings, "fast", [raw(1)])
    summary = await CollectionService(db, settings, [Slow(settings, "slow"), fast]).run()
    by = {r.source: r for r in summary.results}
    assert by["slow"].status == "failed" and "Timeout" in by["slow"].error
    assert by["fast"].new == 1
