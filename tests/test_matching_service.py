from __future__ import annotations

import pymupdf
import pytest

from app.ai.job_matcher import JobMatcher
from app.database.repositories import JobRepository, ProfileRepository, SourceRepository
from app.schemas.job import RawJob
from app.schemas.profile import ProfileIn, SkillIn
from app.services.document_service import DocumentService, NotFoundError
from app.services.matching_service import MatchingService
from app.services.profile_service import ProfileError, ProfileService
from app.utils.normalize import normalize_job
from tests.fakes import FakeProvider
from tests.test_ai import GOOD

PROFILE = ProfileIn(
    name="Md. Test",
    years_experience=5,
    target_titles=["Senior Laravel Developer"],
    preferred_locations=["Bangladesh", "Worldwide Remote"],
    job_types=["Full-time"],
    skills=[SkillIn(name=n) for n in ["PHP", "Laravel", "MySQL", "Redis", "Docker"]],
)


async def seed_jobs(db, n=3):
    async with db.session() as s:
        src = await SourceRepository(s).upsert("t", "T", True, None)
        ids = []
        for i in range(n):
            raw = RawJob(
                source="t",
                external_id=str(i),
                title=f"Senior Laravel Developer {i}",
                company=f"C{i}",
                location="Worldwide",
                apply_url=f"https://x.example.com/{i}",
                description="PHP Laravel MySQL Redis Docker, 4+ years",
            )
            job = await JobRepository(s).insert_if_new(normalize_job(raw), src.id)
            ids.append(job.id)
        ids.append(
            (
                await JobRepository(s).insert_if_new(
                    normalize_job(
                        RawJob(
                            source="t",
                            external_id="sales",
                            title="Sales Executive",
                            company="Z",
                            location="Remote",
                            apply_url="https://x.example.com/sales",
                            description="sell things",
                        )
                    ),
                    src.id,
                )
            ).id
        )
        return ids


async def test_matching_run_saves_and_never_rematches(db, tmp_path):
    await ProfileService(db, [], tmp_path).save_manual(PROFILE)
    await seed_jobs(db)
    fake = FakeProvider([GOOD, GOOD, GOOD])
    service = MatchingService(db, JobMatcher([fake], 35), alert_threshold=80)
    s1 = await service.run()
    assert s1.processed == 4 and s1.ai_scored == 3 and s1.rules_only == 1  # sales job prefiltered
    assert len(s1.high_match_ids) == 3 and len(fake.calls) == 3
    s2 = await service.run()
    assert s2.processed == 0 and len(fake.calls) == 3  # no duplicate AI calls

    # changing the profile invalidates matches -> rescored
    await ProfileService(db, [], tmp_path).save_manual(PROFILE.model_copy(update={"headline": "x"}))
    s3 = await MatchingService(db, JobMatcher([], 35)).run()
    assert s3.processed == 4
    async with db.session() as s:
        assert len(s.all("matches")) == 8  # old-version matches are cleaned up nightly


async def test_matching_without_profile(db):
    assert (await MatchingService(db, JobMatcher([], 35)).run()).processed == 0


def _pdf(text: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 72), text, fontsize=10)
    return doc.tobytes()


async def test_cv_upload_never_overwrites_verified_profile(db, tmp_path):
    svc = ProfileService(db, [], tmp_path / "uploads")
    await svc.save_manual(PROFILE)
    cv = _pdf("Some Person\nPython Backend Developer\n7 years of experience with Python, FastAPI, Kafka, Laravel")
    profile, extraction = await svc.upload_cv("../../etc/passwd.pdf", cv)
    assert profile.name == "Md. Test" and profile.is_verified  # untouched
    assert profile.pending_profile and {s.name for s in extraction.skills} >= {"Python", "FastAPI", "Kafka"}
    assert profile.cv_filename == "passwd.pdf"
    stored = list((tmp_path / "uploads").iterdir())
    assert len(stored) == 1 and stored[0].name.startswith("cv_")

    confirmed = await svc.confirm_pending()
    names = {s.name for s in confirmed.skills}
    assert {"PHP", "Python", "Kafka"} <= names  # merged
    assert confirmed.pending_profile is None and confirmed.years_experience == 7
    with pytest.raises(ProfileError):
        await svc.confirm_pending()


async def test_cv_upload_creates_unverified_profile_when_none(db, tmp_path):
    svc = ProfileService(db, [], tmp_path)
    profile, _ = await svc.upload_cv("cv.pdf", _pdf("Jane Doe\nSenior PHP Developer\n6 years of experience PHP"))
    assert profile.is_verified is False and profile.name == "Jane Doe"


async def test_document_generation_saved(db, tmp_path):
    ids = await seed_jobs(db, 1)
    with pytest.raises(NotFoundError):
        await DocumentService(db, [FakeProvider(["x"])]).generate(ids[0], "cover_letter")
    await ProfileService(db, [], tmp_path).save_manual(PROFILE)
    doc = await DocumentService(db, [FakeProvider(["Dear hiring team, ..."])]).generate(ids[0], "cover_letter")
    async with db.session() as s:
        (row,) = s.all("documents")
    assert row.id == doc.id and row.content.startswith("Dear") and row.provider == "fake"
    async with db.session() as s:
        assert (await ProfileRepository(s).get_active()).name == "Md. Test"
