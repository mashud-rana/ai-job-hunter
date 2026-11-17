"""Arbeitnow free job board API — https://www.arbeitnow.com/blog/job-board-api"""

from __future__ import annotations

from app.collectors.base import BaseCollector
from app.schemas.job import RawJob
from app.utils.normalize import infer_remote_type, parse_datetime

API_URL = "https://www.arbeitnow.com/api/job-board-api"
MAX_PAGES = 3


class ArbeitnowCollector(BaseCollector):
    key = "arbeitnow"
    name = "Arbeitnow"
    targeted = False
    min_interval_hours = 3.0

    async def collect(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self.http.get_json(API_URL, params={"page": page})
            for item in data.get("data", []):
                job = self.parse(item)
                if job:
                    jobs.append(job)
            if not (data.get("links") or {}).get("next"):
                break
        return jobs

    def parse(self, item: dict) -> RawJob | None:
        if not item.get("slug") or not item.get("url"):
            return None
        remote = item.get("remote")
        return RawJob(
            source=self.key,
            external_id=str(item["slug"]),
            title=item.get("title") or "",
            company=item.get("company_name"),
            location=item.get("location"),
            job_type=", ".join(item.get("job_types") or []) or None,
            description=item.get("description") or "",
            salary=None,
            apply_url=item["url"],
            posted_at=parse_datetime(item.get("created_at")),
            tags=[str(t) for t in item.get("tags") or []],
            # Arbeitnow's boolean is authoritative: not remote => on-site, unless the listing says hybrid.
            remote_hint="remote" if remote is True else ("hybrid" if _is_hybrid(item) else "onsite"),
        )


def _is_hybrid(item: dict) -> bool:
    text = " ".join([item.get("title") or "", item.get("location") or "", " ".join(item.get("tags") or [])])
    return infer_remote_type(text) == "hybrid"
