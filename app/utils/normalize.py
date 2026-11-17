"""Turn heterogeneous RawJob records into validated NormalizedJob records."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from dateutil import parser as dateparser

from app.schemas.job import NormalizedJob, RawJob, RemoteType
from app.utils.hashing import content_hash
from app.utils.text import clean_text, html_to_text, truncate
from app.utils.urls import canonicalize_url, is_valid_public_url

MAX_DESCRIPTION = 20_000

_REMOTE = re.compile(r"\b(remote|anywhere|worldwide|work from home|wfh|distributed|telecommute)\b", re.I)
_HYBRID = re.compile(r"\bhybrid\b", re.I)
_ONSITE = re.compile(r"\b(on[- ]?site|in[- ]office|office based)\b", re.I)

COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "Bangladesh": (
        "bangladesh",
        "dhaka",
        "chittagong",
        "chattogram",
        "sylhet",
        "khulna",
        "rajshahi",
        "gazipur",
    ),
    "United States": ("united states", "usa", "u.s.", " us ", "new york", "california", "san francisco"),
    "United Kingdom": ("united kingdom", " uk ", "london", "england", "scotland", "manchester"),
    "Germany": (
        "germany",
        "deutschland",
        "berlin",
        "munich",
        "münchen",
        "hamburg",
        "frankfurt",
        "köln",
        "cologne",
        "bonn",
        "stuttgart",
        "düsseldorf",
        "leipzig",
        "dresden",
        "nürnberg",
        "hannover",
    ),
    "Netherlands": ("netherlands", "amsterdam", "rotterdam"),
    "Switzerland": ("switzerland", "schweiz", "zürich", "zurich", "geneva", "genève", "basel", "bern", "lausanne"),
    "Austria": ("austria", "österreich", "vienna", "wien", "graz", "linz"),
    "Canada": ("canada", "toronto", "vancouver", "montreal"),
    "India": ("india", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune"),
    "Singapore": ("singapore",),
    "United Arab Emirates": ("united arab emirates", "uae", "dubai", "abu dhabi"),
    "Australia": ("australia", "sydney", "melbourne"),
    "Malaysia": ("malaysia", "kuala lumpur"),
    "Japan": ("japan", "tokyo"),
    "Poland": ("poland", "warsaw", "krakow"),
    "Spain": ("spain", "madrid", "barcelona"),
    "France": ("france", "paris"),
}

_SALARY_NUM = re.compile(r"(\d[\d,.]*)\s*([kK])?")
_CURRENCY = {
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "৳": "BDT",
    "bdt": "BDT",
    "tk": "BDT",
    "taka": "BDT",
    "inr": "INR",
    "₹": "INR",
}
_NOT_SPECIFIED = {"", "not specified", "n/a", "na", "negotiable", "none", "competitive", "-"}


_GENERIC_REMOTE = re.compile(r"^\W*(remote|remote job|fully remote|100% remote|anywhere|worldwide)\W*$", re.I)


class InvalidJobError(ValueError):
    pass


def infer_remote_type(*texts: str | None) -> RemoteType:
    blob = " ".join(t for t in texts if t)
    if _HYBRID.search(blob):
        return "hybrid"
    if _REMOTE.search(blob):
        return "remote"
    if _ONSITE.search(blob):
        return "onsite"
    return "unknown"


def infer_country(location: str | None) -> str | None:
    if not location:
        return None
    loc = f" {location.lower()} "
    for country, aliases in COUNTRY_ALIASES.items():
        if any(a in loc for a in aliases):
            return country
    if re.search(r"\b(worldwide|anywhere|global)\b", loc):
        return "Worldwide"
    return None


def parse_datetime(value: object) -> datetime | None:
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        elif isinstance(value, int | float):
            dt = datetime.fromtimestamp(float(value), tz=UTC)
        else:
            dt = dateparser.parse(str(value))
    except (ValueError, OverflowError, OSError, TypeError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    if not text or text.strip().lower() in _NOT_SPECIFIED:
        return None, None, None
    lowered = text.lower()
    currency = next((code for sym, code in _CURRENCY.items() if sym in lowered), None)
    numbers: list[float] = []
    for num, k in _SALARY_NUM.findall(text):
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        if k:
            value *= 1000
        if value >= 100:  # skip things like "5+ years"
            numbers.append(value)
    if not numbers:
        return None, None, currency
    return min(numbers), max(numbers), currency


def normalize_job(raw: RawJob, now: datetime | None = None) -> NormalizedJob:
    """Validate + normalize. Raises InvalidJobError for unusable records."""
    now = now or datetime.now(UTC)
    title = clean_text(raw.title)
    if len(title) < 3:
        raise InvalidJobError("missing title")
    if not raw.external_id:
        raise InvalidJobError("missing external_id")
    apply_url = raw.apply_url.strip()
    if not is_valid_public_url(apply_url):
        raise InvalidJobError(f"invalid apply_url: {apply_url[:100]}")

    company = clean_text(raw.company) or None
    location = clean_text(raw.location) or None
    description = truncate(html_to_text(raw.description), MAX_DESCRIPTION)

    posted_at = parse_datetime(raw.posted_at)
    if posted_at and posted_at > now + timedelta(days=1):
        posted_at = None  # bogus future date

    salary_text = clean_text(raw.salary) or None
    if salary_text and salary_text.lower() in _NOT_SPECIFIED:
        salary_text = None
    s_min, s_max, currency = parse_salary(salary_text)
    if raw.salary_min is not None or raw.salary_max is not None:
        s_min, s_max = raw.salary_min, raw.salary_max
    currency = raw.currency or currency

    remote_type = raw.remote_hint or infer_remote_type(location, title, raw.job_type, " ".join(raw.tags))
    country = infer_country(location)
    if country is None and remote_type == "remote" and (not location or _GENERIC_REMOTE.match(location)):
        country = "Worldwide"  # only when the posting doesn't name a concrete place

    return NormalizedJob(
        source=raw.source,
        external_id=str(raw.external_id)[:255],
        title=truncate(title, 500),
        company=truncate(company, 255) if company else None,
        location=truncate(location, 255) if location else None,
        country=country,
        remote_type=remote_type,
        job_type=truncate(clean_text(raw.job_type), 64) or None,
        description=description,
        salary_text=truncate(salary_text, 255) if salary_text else None,
        salary_min=s_min,
        salary_max=s_max,
        currency=(currency or None) and currency[:8].upper(),
        apply_url=apply_url[:2048],
        canonical_url=canonicalize_url(apply_url)[:2048],
        content_hash=content_hash(title, company, location),
        tags=[truncate(clean_text(t), 60) for t in raw.tags if t and clean_text(t)][:30],
        posted_at=posted_at,
        collected_at=raw.collected_at or now,
        expires_at=parse_datetime(raw.expires_at),
        raw=raw.raw,
    )


def is_expired(
    job_posted_at: datetime | None,
    expires_at: datetime | None,
    max_age_days: int,
    now: datetime | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    if expires_at and expires_at < now:
        return True
    return bool(job_posted_at and job_posted_at < now - timedelta(days=max_age_days))
