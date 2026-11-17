"""Company career pages through official public job-board APIs and RSS feeds.

Configure with CAREER_SOURCES, a JSON list, e.g.
  [{"kind": "greenhouse", "identifier": "gitlab"},
   {"kind": "lever", "identifier": "netflix"},
   {"kind": "ashby", "identifier": "ashby"},
   {"kind": "rss", "identifier": "https://example.com/careers.rss", "company": "Example"}]
"""

from __future__ import annotations

import asyncio
import html
from urllib.parse import quote

import feedparser

from app.collectors.base import Availability, BaseCollector
from app.config import CareerSource
from app.logging import get_logger
from app.schemas.job import RawJob
from app.utils.normalize import parse_datetime
from app.utils.urls import resolves_to_public

log = get_logger(__name__)

GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{id}/jobs"
LEVER = "https://api.lever.co/v0/postings/{id}"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{id}"
WORKPLACE = {"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}


class CareerPagesCollector(BaseCollector):
    key = "careers"
    name = "Company career pages"
    targeted = False

    def availability(self) -> Availability:
        if not self.settings.career_sources:
            return Availability.missing("CAREER_SOURCES not configured")
        return Availability.ok()

    async def collect(self) -> list[RawJob]:
        sources = self.settings.career_sources
        results = await asyncio.gather(*(self._collect_one(s) for s in sources), return_exceptions=True)
        jobs: list[RawJob] = []
        failures = 0
        for src, res in zip(sources, results, strict=True):
            if isinstance(res, BaseException):
                failures += 1
                log.warning("career_source_failed", kind=src.kind, identifier=src.identifier, error=str(res))
                continue
            jobs.extend(res)
        if sources and failures == len(sources):
            raise RuntimeError("all career sources failed")
        return jobs

    async def _collect_one(self, src: CareerSource) -> list[RawJob]:
        ident = quote(src.identifier, safe="")
        if src.kind == "greenhouse":
            data = await self.http.get_json(GREENHOUSE.format(id=ident), params={"content": "true"})
            return [j for j in (self.parse_greenhouse(i, src) for i in data.get("jobs", [])) if j]
        if src.kind == "lever":
            data = await self.http.get_json(LEVER.format(id=ident), params={"mode": "json"})
            return [j for j in (self.parse_lever(i, src) for i in data) if j]
        if src.kind == "ashby":
            data = await self.http.get_json(ASHBY.format(id=ident), params={"includeCompensation": "true"})
            return [j for j in (self.parse_ashby(i, src) for i in data.get("jobs", [])) if j]
        if src.kind == "rss":
            # feed URL is operator-configured, but still guard against internal targets
            if not await resolves_to_public(src.identifier):
                raise ValueError("RSS URL is not a public http(s) URL")
            text = await self.http.get_text(src.identifier)
            return self.parse_rss(text, src)
        raise ValueError(f"unknown career source kind {src.kind}")

    def _ext(self, src: CareerSource, job_id: object) -> str:
        return f"{src.kind}:{src.identifier}:{job_id}"[:255]

    def parse_greenhouse(self, item: dict, src: CareerSource) -> RawJob | None:
        if not item.get("id") or not item.get("absolute_url"):
            return None
        return RawJob(
            source=self.key,
            external_id=self._ext(src, item["id"]),
            title=item.get("title") or "",
            company=src.company or item.get("company_name") or src.identifier,
            location=(item.get("location") or {}).get("name"),
            description=html.unescape(item.get("content") or ""),
            apply_url=item["absolute_url"],
            posted_at=parse_datetime(item.get("first_published") or item.get("updated_at")),
            tags=[d.get("name", "") for d in item.get("departments") or []],
        )

    def parse_lever(self, item: dict, src: CareerSource) -> RawJob | None:
        url = item.get("hostedUrl") or item.get("applyUrl")
        if not item.get("id") or not url:
            return None
        cats = item.get("categories") or {}
        sal = item.get("salaryRange") or {}
        return RawJob(
            source=self.key,
            external_id=self._ext(src, item["id"]),
            title=item.get("text") or "",
            company=src.company or src.identifier,
            location=cats.get("location"),
            job_type=cats.get("commitment"),
            description=item.get("descriptionPlain") or item.get("description") or "",
            salary=f"{sal.get('currency', '')} {sal['min']:,} - {sal['max']:,}".strip()
            if sal.get("min") and sal.get("max")
            else None,
            salary_min=sal.get("min"),
            salary_max=sal.get("max"),
            currency=sal.get("currency"),
            apply_url=url,
            posted_at=parse_datetime(int(item["createdAt"]) / 1000 if item.get("createdAt") else None),
            tags=[t for t in [cats.get("team"), cats.get("department")] if t],
            remote_hint=WORKPLACE.get(str(item.get("workplaceType", "")).lower()),  # type: ignore[arg-type]
        )

    def parse_ashby(self, item: dict, src: CareerSource) -> RawJob | None:
        url = item.get("jobUrl") or item.get("applyUrl")
        if not url or item.get("isListed") is False:
            return None
        comp = (item.get("compensation") or {}).get("compensationTierSummary")
        workplace = str(item.get("workplaceType") or "").lower()
        return RawJob(
            source=self.key,
            external_id=self._ext(src, item.get("id") or url),
            title=item.get("title") or "",
            company=src.company or src.identifier,
            location=item.get("location"),
            job_type=item.get("employmentType"),
            description=item.get("descriptionPlain") or item.get("descriptionHtml") or "",
            salary=comp,
            apply_url=url,
            posted_at=parse_datetime(item.get("publishedAt")),
            tags=[t for t in [item.get("department"), item.get("team")] if t],
            remote_hint="remote" if item.get("isRemote") else WORKPLACE.get(workplace),  # type: ignore[arg-type]
        )

    def parse_rss(self, text: str, src: CareerSource) -> list[RawJob]:
        feed = feedparser.parse(text)
        jobs = []
        for entry in feed.entries:
            link = entry.get("link")
            if not link:
                continue
            jobs.append(
                RawJob(
                    source=self.key,
                    external_id=self._ext(src, entry.get("id") or link),
                    title=entry.get("title") or "",
                    company=src.company or feed.feed.get("title"),
                    location=entry.get("location"),
                    description=entry.get("summary") or entry.get("description") or "",
                    apply_url=link,
                    posted_at=parse_datetime(entry.get("published") or entry.get("updated")),
                    tags=[t.get("term", "") for t in entry.get("tags", [])],
                )
            )
        return jobs
