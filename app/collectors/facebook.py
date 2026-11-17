"""Facebook — marked UNAVAILABLE.

There is no permitted, supported access method for job posts in Facebook groups or pages:
- The Groups API was deprecated by Meta (April 2024), and group content is private to members.
- The Pages API needs per-page tokens and App Review; it isn't a job search feed.
- Scraping facebook.com violates Meta's Terms and requires bypassing login and anti-bot systems.

So this collector never fetches anything. It is still registered so the source appears in
/api/sources and /stats with a clear reason. If you have a legitimate feed (e.g. your own page
via the Graph API), implement it here and return Availability.ok().
"""

from __future__ import annotations

from app.collectors.base import Availability, BaseCollector
from app.schemas.job import RawJob

REASON = (
    "No permitted access method: Meta's Groups API is deprecated and scraping Facebook violates its Terms of Service."
)


class FacebookCollector(BaseCollector):
    key = "facebook"
    name = "Facebook (public job sources)"

    def availability(self) -> Availability:
        return Availability.missing(REASON)

    async def collect(self) -> list[RawJob]:
        raise RuntimeError(REASON)
