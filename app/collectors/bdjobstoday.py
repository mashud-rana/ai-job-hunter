"""BDJobsToday — Bangladeshi job circulars (newspapers, government and private employers).

Access check (2026-11-17): robots.txt allows generic crawlers on listing pages (it only disallows
/admin/, /cgi-bin/, /editor/, /images/, /sort/, /user/), and the site publishes no terms of use.
There is no API or RSS feed, so we read the public category listing pages only. We don't fetch
detail pages: circular text there is an image, and skipping them keeps our load to a couple of
requests per run (at most every 6 hours, 1 request/second per host via HttpClient).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from app.collectors.base import BaseCollector
from app.logging import get_logger
from app.schemas.job import RawJob
from app.utils.normalize import parse_datetime
from app.utils.text import clean_text

log = get_logger(__name__)

BASE = "https://www.bdjobstoday.com/"
CATEGORY_URL = BASE + "jobsbycategory.php"
JOB_LINK = re.compile(r"(?:job_details|hotjobs_details)\.php\?id=(\d+)")
DEADLINE = re.compile(r"Deadline\s*:?\s*([0-9]{1,2}\s+[A-Za-z]{3,9},?\s+[0-9]{4})", re.I)
POSTING = re.compile(r"Post(?:ing|ed)(?:\s+On)?\s*:?\s*([0-9]{1,2}\s+[A-Za-z]{3,9},?\s+[0-9]{4})", re.I)

# BD circular titles rarely name a framework ("Assistant Programmer", "Software Engineer").
RELEVANCE_KEYWORDS = [
    "software",
    "developer",
    "programmer",
    "web",
    "php",
    "laravel",
    "python",
    "backend",
    "back end",
    "full stack",
    "fullstack",
    "devops",
    "system analyst",
    "database",
    "application",
    "ict",
    "it officer",
    "it executive",
    "it manager",
    "technical lead",
    "tech lead",
    "engineer (it)",
    "engineer (software)",
]


# Roles that match "ICT"/"programmer" keywords but aren't software jobs.
EXCLUDE_TITLES = re.compile(
    r"\b(teacher|lecturer|instructor|professor|trainer|demonstrator|data entry|operator)\b", re.I
)


class BDJobsTodayCollector(BaseCollector):
    key = "bdjobstoday"
    name = "BDJobsToday (Bangladesh circulars)"
    targeted = False
    min_interval_hours = 6.0
    relevance_keywords = RELEVANCE_KEYWORDS

    async def collect(self) -> list[RawJob]:
        jobs: dict[str, RawJob] = {}
        failures = 0
        categories = self.settings.bdjobstoday_categories
        for cat in categories:
            try:
                html = await self.http.get_text(CATEGORY_URL, params={"cat": cat})
            except Exception as exc:
                failures += 1
                log.warning("bdjobstoday_category_failed", category=cat, error=str(exc)[:200])
                continue
            page_url = f"{CATEGORY_URL}?cat={cat}"
            for job in parse_listing(html, page_url, self.key):
                if not EXCLUDE_TITLES.search(job.title):
                    jobs.setdefault(job.external_id, job)
        if categories and failures == len(categories):
            raise RuntimeError("all BDJobsToday category pages failed")
        return list(jobs.values())


def _row_text(link: Tag) -> str:
    row = link.find_parent("tr") or link.find_parent(["li", "div"])
    return row.get_text(" ", strip=True) if row is not None else ""


def parse_listing(html: str, page_url: str, source: str = "bdjobstoday") -> list[RawJob]:
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, RawJob] = {}
    for link in soup.find_all("a", href=JOB_LINK):
        href = link["href"]
        m = JOB_LINK.search(href)
        title = clean_text(link.get_text(" "))
        if not m or not title or len(title) < 3:
            continue
        job_id = m.group(1)
        if job_id in found:
            continue
        cell = link.find_parent("td") or link.parent
        company_el = cell.find(class_="HotJobsCompany") if cell is not None else None
        company = clean_text(company_el.get_text(" ")) if company_el else None
        text = _row_text(link)
        deadline = DEADLINE.search(text)
        posted = POSTING.search(text)
        absolute = urljoin(page_url, href)
        qid = parse_qs(urlsplit(absolute).query).get("id", [job_id])[0]
        found[job_id] = RawJob(
            source=source,
            external_id=job_id,
            title=title,
            company=company or None,
            location="Bangladesh",
            description="",  # the circular itself is an image on the detail page
            salary=None,
            apply_url=f"{BASE}job_details.php?id={qid}",
            posted_at=parse_datetime(posted.group(1)) if posted else None,
            expires_at=parse_datetime(deadline.group(1)) if deadline else None,
            remote_hint="onsite",
            tags=["Bangladesh"],
        )
    return list(found.values())
