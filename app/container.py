"""Process-wide dependency container (composition root).

Everything that talks to the outside world (DB, HTTP, AI providers, Telegram) is created here once
and injected into services, so tests can swap any piece.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.ai.job_matcher import JobMatcher
from app.ai.providers import LLMProvider, build_provider_chain
from app.collectors.base import BaseCollector
from app.collectors.registry import build_collectors
from app.config import Settings
from app.database.store import JsonStore
from app.logging import get_logger
from app.services.application_service import ApplicationService
from app.services.collection_service import CollectionService
from app.services.document_service import DocumentService
from app.services.matching_service import MatchingService
from app.services.notification_service import MessageSender, NotificationService, TelegramSender
from app.services.profile_service import ProfileService
from app.services.settings_service import SettingsService
from app.services.stats_service import StatsService
from app.utils.http import HttpClient

log = get_logger(__name__)


@dataclass
class Services:
    collection: CollectionService
    matching: MatchingService
    profiles: ProfileService
    documents: DocumentService
    applications: ApplicationService
    settings: SettingsService
    stats: StatsService
    notifications: NotificationService


@dataclass
class Container:
    settings: Settings
    db: JsonStore
    http: HttpClient
    providers: list[LLMProvider]
    collectors: list[BaseCollector]
    services: Services
    telegram_api: Any = None  # telegram.Bot used for outbound messages
    extras: dict[str, Any] = field(default_factory=dict)  # scheduler, bot (runtime components)

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        db: JsonStore | None = None,
        http: HttpClient | None = None,
        providers: list[LLMProvider] | None = None,
        collectors: list[BaseCollector] | None = None,
        sender: MessageSender | None = None,
    ) -> Container:
        db = db or JsonStore(settings)
        http = http or HttpClient(
            timeout=settings.http_timeout,
            max_retries=settings.http_max_retries,
            rate_per_host=settings.http_rate_per_host,
        )
        providers = build_provider_chain(settings) if providers is None else providers
        collectors = build_collectors(settings, http) if collectors is None else collectors

        telegram_api = None
        if sender is None and settings.telegram_enabled:
            from telegram import Bot

            telegram_api = Bot(settings.telegram_bot_token)
            sender = TelegramSender(telegram_api)

        settings_service = SettingsService(db, settings)
        services = Services(
            collection=CollectionService(db, settings, collectors),
            matching=MatchingService(
                db,
                JobMatcher(providers, settings.prefilter_min_score),
                alert_threshold=settings.alert_threshold,
                max_per_run=settings.max_ai_matches_per_run,
            ),
            profiles=ProfileService(db, providers, settings.uploads_dir),
            documents=DocumentService(db, providers),
            applications=ApplicationService(db),
            settings=settings_service,
            stats=StatsService(db),
            notifications=NotificationService(db, sender, settings.telegram_allowed_chat_ids, settings_service),
        )
        log.info(
            "container_built",
            ai_providers=[p.name for p in providers] or ["rules-only"],
            telegram=bool(sender),
        )
        return cls(settings, db, http, providers, collectors, services, telegram_api)

    async def startup(self) -> None:
        if not self.db.loaded:
            await self.db.load()
        if self.telegram_api is not None:
            await self.telegram_api.initialize()

    async def aclose(self) -> None:
        if self.telegram_api is not None:
            try:
                await self.telegram_api.shutdown()
            except Exception:
                log.warning("telegram_api_shutdown_failed")
        for provider in self.providers:
            await provider.aclose()
        await self.http.aclose()
        await self.db.close()  # final flush of the JSON files
