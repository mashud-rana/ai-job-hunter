"""User-adjustable runtime preferences stored in system_settings (editable from Telegram /settings)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.config import Settings
from app.database.repositories import SettingsRepository
from app.database.store import JsonStore

KEY = "notification_preferences"


class NotificationPreferences(BaseModel):
    alerts_enabled: bool = True
    digest_enabled: bool = True
    alert_threshold: int = Field(default=80, ge=50, le=100)
    digest_size: int = Field(default=10, ge=1, le=30)


class SettingsService:
    def __init__(self, db: JsonStore, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def defaults(self) -> NotificationPreferences:
        return NotificationPreferences(alert_threshold=max(50, min(100, self.settings.alert_threshold)))

    async def get(self) -> NotificationPreferences:
        async with self.db.session() as session:
            raw = await SettingsRepository(session).get(KEY)
        if not raw:
            return self.defaults()
        try:
            return NotificationPreferences.model_validate({**self.defaults().model_dump(), **raw})
        except ValueError:
            return self.defaults()

    async def update(self, **changes: object) -> NotificationPreferences:
        current = await self.get()
        updated = NotificationPreferences.model_validate({**current.model_dump(), **changes})
        async with self.db.session() as session:
            await SettingsRepository(session).set(KEY, updated.model_dump())
        return updated
