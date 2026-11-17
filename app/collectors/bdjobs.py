"""BDJobs via job-alert / newsletter emails.

BDJobs offers no public API or RSS feed, and its site terms forbid automated harvesting, so we only
read the alert emails the user subscribes to (My Bdjobs → Job Alert / Email subscriptions).
Job ids come from jobdetails links (``jobdetails.asp?id=123``, ``/jobdetails/123``, ``/details/123``).
Set BDJOBS_ALERT_SENDERS if your alerts arrive from a different address.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from app.collectors.email_alerts import AlertEmail, EmailAlertCollector
from app.schemas.job import RawJob
from app.utils.normalize import parse_datetime
from app.utils.text import clean_text

BDJOBS_HOST = re.compile(r"(^|\.)bdjobs\.com$", re.I)
PATH_ID = re.compile(r"/(?:jobdetails?|details)/(?:[^/]*/)?(\d{5,})", re.I)
NOISE = re.compile(r"^(view details|apply now|apply|details|click here|view all jobs|unsubscribe)$", re.I)


def _job_id(href: str) -> str | None:
    try:
        parts = urlsplit(href)
    except ValueError:
        return None
    if not parts.hostname or not BDJOBS_HOST.search(parts.hostname):
        return None
    qs = parse_qs(parts.query)
    for key in ("id", "ID", "jobid", "JobId"):
        if qs.get(key) and qs[key][0].isdigit():
            return qs[key][0]
    m = PATH_ID.search(parts.path)
    return m.group(1) if m else None


def parse_bdjobs_alert(html: str, received_at=None, source: str = "bdjobs") -> list[RawJob]:
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, RawJob] = {}
    for link in soup.find_all("a", href=True):
        job_id = _job_id(link["href"])
        if not job_id:
            continue
        title = clean_text(link.get_text(" "))
        if job_id in found or not title or NOISE.match(title) or len(title) < 4:
            continue
        company = location = deadline = None
        row = link.find_parent(["tr", "td", "div", "table"])
        if row is not None:
            lines = [ln for ln in (clean_text(x) for x in row.get_text("\n").split("\n")) if ln]
            rest = [ln for ln in lines if ln != title and not NOISE.match(ln)]
            for ln in rest:
                low = ln.lower()
                if low.startswith("deadline"):
                    deadline = ln.split(":", 1)[-1].strip()
                elif low.startswith("location"):
                    location = ln.split(":", 1)[-1].strip()
                elif company is None and not low.startswith(("experience", "salary", "education")):
                    company = ln
        found[job_id] = RawJob(
            source=source,
            external_id=job_id,
            title=title,
            company=company,
            location=location or "Bangladesh",
            description="",
            salary=None,
            apply_url=f"https://jobs.bdjobs.com/jobdetails.asp?id={job_id}",
            posted_at=received_at,
            expires_at=parse_datetime(deadline) if deadline else None,
        )
    return list(found.values())


class BDJobsAlertsCollector(EmailAlertCollector):
    key = "bdjobs"
    name = "BDJobs (alert emails)"

    @property
    def senders(self) -> list[str]:
        return self.settings.bdjobs_alert_senders

    def parse_email(self, alert: AlertEmail) -> list[RawJob]:
        return parse_bdjobs_alert(alert.html or alert.text, alert.received_at, self.key)
