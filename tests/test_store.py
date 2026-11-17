from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.database.backends import GistBackend, LocalBackend, StorageError
from app.database.models import JobMatch
from app.database.repositories import ApplicationRepository, JobRepository, ProfileRepository, SourceRepository
from app.database.store import JsonStore
from app.scheduler import tasks
from app.schemas.job import RawJob
from app.utils.normalize import normalize_job


def njob(i: int, **kw):
    data = dict(
        source="t",
        external_id=str(i),
        title=f"Senior Laravel Developer {i}",
        company=f"C{i}",
        location="Remote",
        apply_url=f"https://jobs.example.com/{i}",
        description="PHP Laravel",
    )
    data.update(kw)
    return normalize_job(RawJob(**data))


async def seed(store: JsonStore) -> tuple[int, int]:
    async with store.session() as s:
        src = await SourceRepository(s).upsert("t", "T", True, None)
        job = await JobRepository(s).insert_if_new(njob(1), src.id)
        p = await ProfileRepository(s).create({"name": "Me", "years_experience": 5}, [{"name": "PHP"}], True)
        s.add(
            JobMatch(
                job_id=job.id,
                profile_id=p.id,
                profile_version=p.version,
                match_score=88,
                category="strong",
                recommendation="apply",
            )
        )
        await ApplicationRepository(s).create(
            {"job_id": job.id, "position": job.title, "status": "applied", "applied_at": datetime.now(UTC)}
        )
    return src.id, job.id


async def test_roundtrip_reload_from_disk(settings):
    store = JsonStore(settings)
    await store.load()
    _, job_id = await seed(store)
    await store.close()

    files = sorted(p.name for p in settings.data_dir.glob("*.json"))
    assert {"jobs.json", "matches.json", "profiles.json", "applications.json", "sources.json", "state.json"} <= set(
        files
    )
    assert not list(settings.data_dir.glob("*.tmp"))  # atomic writes leave no temp files

    again = JsonStore(settings)
    await again.load()
    async with again.session() as s:
        job = await JobRepository(s).get(job_id)
        assert job.source.key == "t" and job.collected_at.tzinfo is not None
        ref = await ProfileRepository(s).active_ref()
        match = await JobRepository(s).get_match(job_id, ref)
        assert match.match_score == 88 and match.job is job  # references re-linked
        app_ = (await ApplicationRepository(s).list_applications()).items[0]
        assert app_.job is job and app_.applied_at.tzinfo is not None
        profile = await ProfileRepository(s).get_active()
        assert [sk.name for sk in profile.skills] == ["PHP"]
        # ids keep increasing after reload
        src = await SourceRepository(s).upsert("u", "U", True, None)
        assert src.id == 2
    await again.close()


async def test_seen_keys_block_reimport_after_prune(settings, db):
    async with db.session() as s:
        src = await SourceRepository(s).upsert("t", "T", True, None)
        job = await JobRepository(s).insert_if_new(njob(1), src.id)
        job.is_expired = True
        job.collected_at = datetime.now(UTC) - timedelta(days=60)
        assert await JobRepository(s).prune(keep_expired_days=14, max_jobs=100) == 1
        assert s.all("jobs") == []
        assert await JobRepository(s).insert_if_new(njob(1), src.id) is None  # remembered


async def test_prune_caps_total_but_keeps_tracked_jobs(settings, db):
    async with db.session() as s:
        src = await SourceRepository(s).upsert("t", "T", True, None)
        ids = [(await JobRepository(s).insert_if_new(njob(i), src.id)).id for i in range(5)]
        await ApplicationRepository(s).create({"job_id": ids[0], "position": "x"})
        removed = await JobRepository(s).prune(keep_expired_days=14, max_jobs=3)
        assert removed == 2
        remaining = {j.id for j in s.all("jobs")}
        assert ids[0] in remaining and len(remaining) == 3  # oldest removed, tracked one kept


async def test_flush_failure_keeps_data_and_retries(settings):
    class Flaky(LocalBackend):
        fail = True

        async def write(self, files):
            if self.fail:
                raise StorageError("disk full")
            await super().write(files)

    backend = Flaky(settings.data_dir)
    store = JsonStore(settings, backend)
    await store.load()
    async with store.session() as s:
        await SourceRepository(s).upsert("t", "T", True, None)
    assert store.last_error == "disk full"
    backend.fail = False
    assert await store.flush(force=True)
    assert store.last_error is None
    assert json.loads((settings.data_dir / "sources.json").read_text())[0]["key"] == "t"


async def test_gist_backend_read_write_and_debounce(settings, mock_http):
    gist = {
        "files": {
            "sources.json": {"content": json.dumps([{"id": 1, "key": "t", "name": "T"}]), "truncated": False},
            "jobs.json": {
                "content": "[",
                "truncated": True,
                "raw_url": "https://gist.githubusercontent.com/raw/jobs.json",
            },
            "README.md": {"content": "ignore me"},
        }
    }
    mock_http.get("https://api.github.com/gists/abc").mock(return_value=httpx.Response(200, json=gist))
    mock_http.get("https://gist.githubusercontent.com/raw/jobs.json").mock(return_value=httpx.Response(200, text="[]"))
    patch = mock_http.patch("https://api.github.com/gists/abc").mock(return_value=httpx.Response(200, json={}))

    settings.storage_flush_seconds = 3600
    backend = GistBackend("abc", "ghp_secret_token_value_123456")
    store = JsonStore(settings, backend)
    await store.load()
    assert store.tables["sources"][1].key == "t"  # truncated file fetched via raw_url

    async with store.session() as s:
        await SourceRepository(s).set_enabled("t", False)
    assert patch.call_count == 1  # first write goes through
    async with store.session() as s:
        await SourceRepository(s).set_enabled("t", True)
    assert patch.call_count == 1  # debounced
    await store.close()  # forced flush on shutdown
    assert patch.call_count == 2
    sent = json.loads(patch.calls[-1].request.content)
    assert json.loads(sent["files"]["sources.json"]["content"])[0]["enabled"] is True
    assert patch.calls[-1].request.headers["authorization"] == "Bearer ghp_secret_token_value_123456"


async def test_gist_errors_never_leak_token(mock_http):
    mock_http.get("https://api.github.com/gists/abc").mock(return_value=httpx.Response(401, json={"message": "Bad"}))
    backend = GistBackend("abc", "ghp_secret_token_value_123456")
    with pytest.raises(StorageError) as ei:
        await backend.read_all()
    assert "ghp_" not in str(ei.value)
    await backend.aclose()


def test_gist_requires_credentials():
    with pytest.raises(ValueError):
        GistBackend("", "")


async def test_run_due_tasks_once_per_day(container, sender, monkeypatch):
    calls = {"collect": 0, "maint": 0, "digest": 0}

    async def fake_collect(c, force=False, **_):
        calls["collect"] += 1
        from app.services.collection_service import CollectionSummary

        return {"collection": CollectionSummary([]), "matching": {}, "alerts": 0}

    real_maint, real_digest = tasks.nightly_maintenance, tasks.daily_digest

    async def maint(c, check_urls=True):
        calls["maint"] += 1
        return await real_maint(c, check_urls=False)

    async def digest(c):
        calls["digest"] += 1
        return await real_digest(c)

    monkeypatch.setattr(tasks, "collect_and_match", fake_collect)
    monkeypatch.setattr(tasks, "nightly_maintenance", maint)
    monkeypatch.setattr(tasks, "daily_digest", digest)

    container.settings.digest_hour = 0  # digest is "due" whatever the time
    first = await tasks.run_due_tasks(container)
    second = await tasks.run_due_tasks(container)
    assert first["maintenance"] and first["digest"]
    assert not second["maintenance"] and not second["digest"]
    assert calls == {"collect": 2, "maint": 1, "digest": 1}

    container.settings.digest_hour = 24  # never due
    async with container.db.session() as s:
        s.state["settings"].pop("last_digest_date")
        s.touch("state")
    third = await tasks.run_due_tasks(container)
    assert not third["digest"]


async def test_cron_endpoint(settings, container, monkeypatch):
    from app.main import create_app

    ran = []

    async def fake_due(c, force=False):
        ran.append(force)
        return {"new_jobs": 2, "digest": False, "maintenance": True, "alerts": 0, "matching": {}}

    monkeypatch.setattr(tasks, "run_due_tasks", fake_due)
    app = create_app(settings, container)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://t")
    async with app.router.lifespan_context(app), client as c:
        assert (await c.get("/cron/sync")).status_code == 401
        assert (await c.get("/cron/sync", params={"token": "wrong"})).status_code == 401
        r = await c.get("/cron/sync", params={"token": "sync-secret", "wait": "true"})
        assert r.status_code == 200 and r.json()["new_jobs"] == 2
        r = await c.post("/cron/sync", headers={"X-Sync-Token": "sync-secret"}, params={"force": "true"})
        assert r.status_code == 202
        import asyncio

        await asyncio.sleep(0.05)
        assert ran == [False, True]
        settings.sync_token = ""
        assert (await c.get("/cron/sync", params={"token": "x"})).status_code == 503
