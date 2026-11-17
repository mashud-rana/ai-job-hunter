"""Remotive public API — https://remotive.com/api-documentation

Remotive asks clients not to poll excessively (a few times per day) and to link back to the
original posting; we keep their URL as apply_url and run at most every 6 hours.
"""

from __future__ import annotations

import asyncio

from app.collectors.base import BaseCollector
from app.logging import get_logger
from app.schemas.job import RawJob
from app.utils.normalize import parse_datetime

log = get_logger(__name__)

API_URL = "https://remotive.com/api/remote-jobs"
JOB_TYPES = {
    "full_time": "Full-time",
    "contract": "Contract",
    "part_time": "Part-time",
    "freelance": "Freelance",
    "internship": "Internship",
}


class RemotiveCollector(BaseCollector):
    key = "remotive"
    name = "Remotive"
    targeted = False  # Remotive search is fuzzy; keep the relevance filter on
    min_interval_hours = 6.0

    async def collect(self) -> list[RawJob]:
        jobs: dict[str, RawJob] = {}
        responses = await asyncio.gather(
            *(self.http.get_json(API_URL, params={"search": kw, "limit": 50}) for kw in self.settings.search_keywords),
            return_exceptions=True,
        )
        failures = 0
        for kw, data in zip(self.settings.search_keywords, responses, strict=True):
            if isinstance(data, BaseException):
                failures += 1
                log.warning("remotive_query_failed", keyword=kw, error=str(data))
                continue
            for item in data.get("jobs", []):
                job = self.parse(item)
                if job:
                    jobs[job.external_id] = job
        if failures and failures == len(responses):
            raise RuntimeError("all Remotive queries failed")
        return list(jobs.values())

    def parse(self, item: dict) -> RawJob | None:
        if not item.get("id") or not item.get("url"):
            return None
        return RawJob(
            source=self.key,
            external_id=str(item["id"]),
            title=item.get("title") or "",
            company=item.get("company_name"),
            location=item.get("candidate_required_location") or "Remote",
            job_type=JOB_TYPES.get(item.get("job_type") or "", item.get("job_type")),
            description=item.get("description") or "",
            salary=item.get("salary") or None,
            apply_url=item["url"],
            posted_at=parse_datetime(item.get("publication_date")),
            tags=[str(t) for t in item.get("tags") or []],
            remote_hint="remote",
            raw={"category": item.get("category")},
        )
