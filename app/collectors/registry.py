from __future__ import annotations

from app.collectors.adzuna import AdzunaCollector
from app.collectors.arbeitnow import ArbeitnowCollector
from app.collectors.base import BaseCollector
from app.collectors.bdjobs import BDJobsAlertsCollector
from app.collectors.bdjobstoday import BDJobsTodayCollector
from app.collectors.careers import CareerPagesCollector
from app.collectors.facebook import FacebookCollector
from app.collectors.linkedin import LinkedInAlertsCollector
from app.collectors.remoteok import RemoteOKCollector
from app.collectors.remotive import RemotiveCollector
from app.config import Settings
from app.utils.http import HttpClient

COLLECTOR_CLASSES: list[type[BaseCollector]] = [
    RemotiveCollector,
    RemoteOKCollector,
    ArbeitnowCollector,
    AdzunaCollector,
    LinkedInAlertsCollector,
    BDJobsAlertsCollector,
    BDJobsTodayCollector,
    CareerPagesCollector,
    FacebookCollector,
]


def build_collectors(settings: Settings, http: HttpClient) -> list[BaseCollector]:
    return [cls(settings, http) for cls in COLLECTOR_CLASSES]
