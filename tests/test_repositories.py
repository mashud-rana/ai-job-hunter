from datetime import UTC, datetime, timedelta

from app.database.models import JobMatch
from app.database.repositories import (
    ApplicationRepository,
    JobFilters,
    JobRepository,
    MatchRepository,
    ProfileRepository,
    SettingsRepository,
    SourceRepository,
)
from app.schemas.job import RawJob
from app.utils.normalize import normalize_job


def njob(i: int, **kw):
    base = dict(
        source="remotive",
        external_id=str(i),
        title=f"Senior Laravel Developer {i}",
        company=f"Co{i}",
        location="Remote",
        apply_url=f"https://example.com/jobs/{i}",
        description="Laravel PHP MySQL",
    )
    base.update(kw)
    return normalize_job(RawJob(**base))


async def test_insert_and_dedupe(session):
    src = await SourceRepository(session).upsert("remotive", "Remotive", True, None)
    repo = JobRepository(session)
    assert await repo.insert_if_new(njob(1), src.id) is not None
    # same external id
    assert await repo.insert_if_new(njob(1, title="Different"), src.id) is None
    # same canonical url from tracking-param variant
    assert await repo.insert_if_new(njob(2, apply_url="https://www.example.com/jobs/1?utm_source=x"), src.id) is None
    # same content hash cross-posted
    assert (
        await repo.insert_if_new(
            njob(3, title="Senior Laravel Developer 1", company="Co1", apply_url="https://other.com/a"),
            src.id,
        )
        is None
    )
    assert await repo.insert_if_new(njob(4), src.id) is not None


async def test_list_filter_sort_and_matches(session):
    src = await SourceRepository(session).upsert("remotive", "Remotive", True, None)
    jobs = JobRepository(session)
    j1 = await jobs.insert_if_new(njob(1, description="Python FastAPI"), src.id)
    j2 = await jobs.insert_if_new(njob(2, location="Dhaka, Bangladesh"), src.id)
    profile = await ProfileRepository(session).create(
        {"name": "Test", "years_experience": 5}, [{"name": "PHP"}, {"name": "php"}, {"name": "Laravel"}], True
    )
    assert len(profile.skills) == 2  # deduped case-insensitively
    ref = await ProfileRepository(session).active_ref()
    m = await MatchRepository(session).save(
        JobMatch(
            job_id=j2.id,
            profile_id=ref.id,
            profile_version=ref.version,
            match_score=91,
            category="excellent",
            recommendation="apply",
            provider="rules",
        )
    )
    assert m is not None
    # duplicate match for same version rejected
    dup = JobMatch(
        job_id=j2.id,
        profile_id=ref.id,
        profile_version=ref.version,
        match_score=50,
        category="moderate",
        recommendation="consider",
        provider="rules",
    )
    assert await MatchRepository(session).save(dup) is None

    page = await jobs.list_jobs(JobFilters(sort="score"), ref)
    assert page.total == 2
    assert page.items[0][0].id == j2.id and page.items[0][1].match_score == 91

    page = await jobs.list_jobs(JobFilters(skills=["fastapi"]), ref)
    assert [j.id for j, _ in page.items] == [j1.id]
    page = await jobs.list_jobs(JobFilters(country="bangladesh", min_score=80), ref)
    assert [j.id for j, _ in page.items] == [j2.id]
    page = await jobs.list_jobs(JobFilters(q="100%_"), ref)  # LIKE wildcards escaped
    assert page.total == 0

    unmatched = await jobs.unmatched_jobs(ref, 10)
    assert [j.id for j in unmatched] == [j1.id]

    # profile update bumps version -> everything is unmatched again
    p = await ProfileRepository(session).get_active()
    await ProfileRepository(session).update(p, {"headline": "x"}, None)
    ref2 = await ProfileRepository(session).active_ref()
    assert ref2.version == ref.version + 1
    assert len(await jobs.unmatched_jobs(ref2, 10)) == 2


async def test_mark_expired(session):
    src = await SourceRepository(session).upsert("remotive", "Remotive", True, None)
    jobs = JobRepository(session)
    await jobs.insert_if_new(njob(1, posted_at=datetime.now(UTC) - timedelta(days=90)), src.id)
    await jobs.insert_if_new(njob(2), src.id)
    assert await jobs.mark_expired(45) == 1
    stats = await jobs.stats()
    assert stats["total_jobs"] == 2 and stats["active_jobs"] == 1


async def test_applications_and_settings(session):
    apps = ApplicationRepository(session)
    a = await apps.create({"position": "Senior PHP Dev", "company": "Acme", "status": "saved"})
    await apps.update(a, {"status": "applied", "applied_at": datetime.now(UTC)})
    page = await apps.list_applications(["applied"])
    assert page.total == 1 and page.items[0].applied_at.tzinfo is not None
    assert await apps.counts() == {"applied": 1}

    st = SettingsRepository(session)
    assert await st.get("alert_threshold", 80) == 80
    await st.set("alert_threshold", 85)
    await st.set("alert_threshold", 90)
    assert await st.get("alert_threshold") == 90
