"""LinkedIn Job Alerts via email notifications (no LinkedIn scraping, no login).

Setup: create job alerts on LinkedIn for your target titles with email delivery, then set
IMAP_* credentials (for Gmail use an App Password). Parsing is best-effort because LinkedIn's email
template changes over time. Jobs are identified by the numeric id in /jobs/view/<id> links.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

from app.collectors.email_alerts import AlertEmail, EmailAlertCollector
from app.schemas.job import RawJob
from app.utils.text import clean_text

JOB_LINK = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.I)
SEPARATOR = re.compile(r"\s+[·•|-]\s+")
NOISE = re.compile(
    r"^(view job|apply|easy apply|see all jobs|promoted|actively recruiting|\d+ (?:connections?|applicants?)"
    r"|be an early applicant|new|this company is actively hiring|school alum.*|\d+ (?:school )?alumni.*)$",
    re.I,
)


def _lines(node: Tag) -> list[str]:
    text = node.get_text("\n")
    return [ln for ln in (clean_text(x) for x in text.split("\n")) if ln]


def parse_linkedin_alert(html: str, received_at=None, source: str = "linkedin") -> list[RawJob]:
    soup = BeautifulSoup(html, "lxml")
    found: dict[str, RawJob] = {}
    for link in soup.find_all("a", href=True):
        m = JOB_LINK.search(link["href"])
        if not m:
            continue
        job_id = m.group(1)
        title = clean_text(link.get_text(" "))
        if job_id in found or not title or NOISE.match(title) or len(title) < 4:
            continue
        # Walk up to the smallest container that holds the card's text (title + company + location).
        company = location = None
        container: Tag | None = link
        for _ in range(6):
            container = container.parent if container is not None else None
            if container is None:
                break
            lines = _lines(container)
            if len(lines) >= 2 and title in lines:
                after = [ln for ln in lines[lines.index(title) + 1 :] if not NOISE.match(ln)]
                if after:
                    parts = SEPARATOR.split(after[0], maxsplit=1)
                    if len(parts) == 2:
                        company, location = parts
                    else:
                        company = after[0]
                        location = after[1] if len(after) > 1 else None
                    break
        found[job_id] = RawJob(
            source=source,
            external_id=job_id,
            title=title,
            company=company,
            location=location,
            description="",  # alerts don't contain the description
            salary=None,
            apply_url=f"https://www.linkedin.com/jobs/view/{job_id}/",
            posted_at=received_at,
        )
    return list(found.values())


class LinkedInAlertsCollector(EmailAlertCollector):
    key = "linkedin"
    name = "LinkedIn Job Alerts (email)"

    @property
    def senders(self) -> list[str]:
        return self.settings.linkedin_alert_senders

    def parse_email(self, alert: AlertEmail) -> list[RawJob]:
        return parse_linkedin_alert(alert.html or alert.text, alert.received_at, self.key)
