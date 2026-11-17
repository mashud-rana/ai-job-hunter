"""Adzuna official API — https://developer.adzuna.com/ (requires ADZUNA_APP_ID / ADZUNA_APP_KEY).

Note: Adzuna returns a description snippet, not the full text; matching works on that snippet.
"""

from __future__ import annotations

import asyncio

from app.collectors.base import Availability, BaseCollector
from app.logging import get_logger
from app.schemas.job import RawJob
from app.utils.normalize import parse_datetime

log = get_logger(__name__)

API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
CONTRACT_TIME = {"full_time": "Full-time", "part_time": "Part-time"}
CONTRACT_TYPE = {"contract": "Contract", "permanent": "Permanent"}
CURRENCY = {
    "gb": "GBP",
    "us": "USD",
    "de": "EUR",
    "nl": "EUR",
    "fr": "EUR",
    "sg": "SGD",
    "in": "INR",
    "au": "AUD",
    "ca": "CAD",
    "pl": "PLN",
    "at": "EUR",
    "it": "EUR",
    "es": "EUR",
}


class AdzunaCollector(BaseCollector):
    key = "adzuna"
    name = "Adzuna"
    targeted = True
    min_interval_hours = 3.0

    def availability(self) -> Availability:
        if not (self.settings.adzuna_app_id and self.settings.adzuna_app_key):
            return Availability.missing("ADZUNA_APP_ID / ADZUNA_APP_KEY not configured")
        return Availability.ok()

    async def collect(self) -> list[RawJob]:
        tasks = []
        combos = [(c, q) for c in self.settings.adzuna_countries for q in self.settings.adzuna_queries]
        for country, query in combos:
            params = {
                "app_id": self.settings.adzuna_app_id,
                "app_key": self.settings.adzuna_app_key,
                "what": query,
                "results_per_page": 50,
                "max_days_old": 7,
                "content-type": "application/json",
            }
            tasks.append(self.http.get_json(API_URL.format(country=country), params=params))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        jobs: dict[str, RawJob] = {}
        failures = 0
        for (country, query), data in zip(combos, results, strict=True):
            if isinstance(data, BaseException):
                failures += 1
                # never log params: they contain the app key
                log.warning("adzuna_query_failed", country=country, query=query, error=type(data).__name__)
                continue
            for item in data.get("results", []):
                job = self.parse(item, country)
                if job:
                    jobs[job.external_id] = job
        if combos and failures == len(combos):
            raise RuntimeError("all Adzuna queries failed")
        return list(jobs.values())

    def parse(self, item: dict, country: str) -> RawJob | None:
        if not item.get("id") or not item.get("redirect_url"):
            return None
        job_type = CONTRACT_TYPE.get(item.get("contract_type") or "") or CONTRACT_TIME.get(
            item.get("contract_time") or ""
        )
        s_min, s_max = item.get("salary_min"), item.get("salary_max")
        currency = CURRENCY.get(country)
        salary = None
        if s_min or s_max:
            salary = f"{currency or ''} {s_min or s_max:,.0f} - {s_max or s_min:,.0f}".strip()
        return RawJob(
            source=self.key,
            external_id=str(item["id"]),
            title=item.get("title") or "",
            company=(item.get("company") or {}).get("display_name"),
            location=(item.get("location") or {}).get("display_name"),
            job_type=job_type,
            description=item.get("description") or "",
            salary=salary,
            salary_min=s_min,
            salary_max=s_max,
            currency=currency if salary else None,
            apply_url=item["redirect_url"],
            posted_at=parse_datetime(item.get("created")),
            tags=[(item.get("category") or {}).get("label", "")],
            raw={"country": country},
        )
