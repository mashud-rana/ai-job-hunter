"""Score unmatched jobs against the active candidate profile."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.ai.job_matcher import JobData, JobMatcher, ProfileData
from app.database.models import CandidateProfile, Job, JobMatch
from app.database.repositories import JobRepository, MatchRepository, ProfileRepository
from app.database.store import JsonStore
from app.logging import get_logger

log = get_logger(__name__)


def profile_to_data(p: CandidateProfile) -> ProfileData:
    return ProfileData(
        name=p.name,
        years_experience=p.years_experience or 0,
        skills=[s.name for s in p.skills],
        target_titles=list(p.target_titles or []),
        preferred_locations=list(p.preferred_locations or []),
        job_types=list(p.job_types or []),
        min_salary=p.min_salary,
        salary_currency=p.salary_currency,
        headline=p.headline,
        summary=p.summary,
    )


def job_to_data(j: Job) -> JobData:
    return JobData(
        title=j.title,
        company=j.company,
        location=j.location,
        country=j.country,
        remote_type=j.remote_type,
        job_type=j.job_type,
        description=j.description or "",
        salary_text=j.salary_text,
        salary_min=j.salary_min,
        salary_max=j.salary_max,
        currency=j.currency,
        tags=list(j.tags or []),
    )


@dataclass
class MatchingSummary:
    processed: int = 0
    ai_scored: int = 0
    rules_only: int = 0
    failed: int = 0
    high_match_ids: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "processed": self.processed,
            "ai_scored": self.ai_scored,
            "rules_only": self.rules_only,
            "failed": self.failed,
            "high_matches": len(self.high_match_ids),
        }


class MatchingService:
    def __init__(
        self,
        db: JsonStore,
        matcher: JobMatcher,
        alert_threshold: int = 80,
        max_per_run: int = 100,
        ai_concurrency: int = 3,
    ) -> None:
        self.db = db
        self.matcher = matcher
        self.alert_threshold = alert_threshold
        self.max_per_run = max_per_run
        self.ai_concurrency = ai_concurrency

    async def run(self) -> MatchingSummary:
        summary = MatchingSummary()
        async with self.db.session() as session:
            profile = await ProfileRepository(session).get_active()
            if profile is None:
                log.warning("matching_skipped", reason="no active candidate profile")
                return summary
            ref = await ProfileRepository(session).active_ref()
            assert ref is not None
            pdata = profile_to_data(profile)
            jobs = await JobRepository(session).unmatched_jobs(ref, self.max_per_run)
            job_data = [(j.id, job_to_data(j)) for j in jobs]

        sem = asyncio.Semaphore(self.ai_concurrency)

        async def score(job_id: int, jd: JobData) -> None:
            async with sem:
                try:
                    outcome = await self.matcher.match(jd, pdata)
                except Exception:
                    summary.failed += 1
                    log.exception("match_failed", job_id=job_id)
                    return
            r = outcome.result
            async with self.db.session() as session:
                saved = await MatchRepository(session).save(
                    JobMatch(
                        job_id=job_id,
                        profile_id=ref.id,
                        profile_version=ref.version,
                        match_score=r.match_score,
                        category=r.category,
                        matched_skills=r.matched_skills,
                        missing_skills=r.missing_skills,
                        experience_match=r.experience_match,
                        location_eligible=r.location_eligible,
                        confidence=r.confidence,
                        ai_reason=r.ai_reason,
                        recommendation=r.recommendation,
                        provider=outcome.provider,
                        model=outcome.model,
                    )
                )
            if saved is None:
                return  # matched concurrently by another run
            summary.processed += 1
            if outcome.provider == "rules":
                summary.rules_only += 1
            else:
                summary.ai_scored += 1
            if r.match_score >= self.alert_threshold:
                summary.high_match_ids.append(saved.id)

        await asyncio.gather(*(score(jid, jd) for jid, jd in job_data))
        log.info("matching_finished", **summary.as_dict())
        return summary
