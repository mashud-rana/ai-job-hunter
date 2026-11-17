"""Collector interface. Add a new source by subclassing BaseCollector and registering it."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.config import Settings
from app.schemas.job import RawJob
from app.utils.http import HttpClient


@dataclass(frozen=True)
class Availability:
    available: bool
    reason: str | None = None

    @classmethod
    def ok(cls) -> Availability:
        return cls(True)

    @classmethod
    def missing(cls, reason: str) -> Availability:
        return cls(False, reason)


class BaseCollector(ABC):
    #: Unique, stable identifier stored in job_sources.key
    key: str
    #: Human readable name
    name: str
    #: True when the source already returns targeted results (search query / personal alerts),
    #: so the generic keyword relevance filter is skipped.
    targeted: bool = False
    #: Minimum hours between runs, to honour source usage guidelines.
    min_interval_hours: float = 0.0
    #: Keywords for the relevance filter; None means use SEARCH_KEYWORDS.
    relevance_keywords: list[str] | None = None

    def __init__(self, settings: Settings, http: HttpClient) -> None:
        self.settings = settings
        self.http = http

    def availability(self) -> Availability:
        """Whether the source can run (credentials present, access method supported)."""
        return Availability.ok()

    @abstractmethod
    async def collect(self) -> list[RawJob]:
        """Fetch jobs from the source. May raise; the engine isolates failures per collector."""


def is_relevant(job: RawJob, keywords: list[str]) -> bool:
    """Cheap keyword gate for broad feeds (Remote OK, Arbeitnow, career boards)."""
    if not keywords:
        return True
    patterns = [re.compile(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])") for k in keywords]
    head = f"{job.title} {' '.join(job.tags)}".lower()
    if any(p.search(head) for p in patterns):
        return True
    # A description-only match must mention at least two distinct target keywords.
    body = job.description[:5000].lower()
    return sum(1 for p in patterns if p.search(body)) >= 2
