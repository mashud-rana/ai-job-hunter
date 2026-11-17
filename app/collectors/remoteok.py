"""Remote OK public API — https://remoteok.com/api

Terms: link back to the Remote OK URL and mention Remote OK as the source. We keep the Remote OK
URL as apply_url and show "via Remote OK" in notifications/dashboard.
"""

from __future__ import annotations

from app.collectors.base import BaseCollector
from app.schemas.job import RawJob
from app.utils.normalize import parse_datetime

API_URL = "https://remoteok.com/api"


class RemoteOKCollector(BaseCollector):
    key = "remoteok"
    name = "Remote OK"
    targeted = False  # full feed; relevance filter applies
    min_interval_hours = 3.0

    async def collect(self) -> list[RawJob]:
        data = await self.http.get_json(API_URL)
        if not isinstance(data, list):
            raise ValueError("unexpected Remote OK response")
        jobs = []
        for item in data:
            if not isinstance(item, dict) or "position" not in item:
                continue  # first element is the legal notice
            job = self.parse(item)
            if job:
                jobs.append(job)
        return jobs

    def parse(self, item: dict) -> RawJob | None:
        url = item.get("url") or item.get("apply_url")
        if not item.get("id") or not url:
            return None
        s_min = _num(item.get("salary_min"))
        s_max = _num(item.get("salary_max"))
        salary = f"${s_min:,.0f} - ${s_max:,.0f}" if s_min and s_max else None
        return RawJob(
            source=self.key,
            external_id=str(item["id"]),
            title=item.get("position") or "",
            company=item.get("company"),
            location=item.get("location") or "Remote",
            description=item.get("description") or "",
            salary=salary,
            salary_min=s_min,
            salary_max=s_max,
            currency="USD" if salary else None,
            apply_url=url,
            posted_at=parse_datetime(item.get("date") or item.get("epoch")),
            tags=[str(t) for t in item.get("tags") or []],
            remote_hint="remote",
        )


def _num(value: object) -> float | None:
    try:
        n = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None
