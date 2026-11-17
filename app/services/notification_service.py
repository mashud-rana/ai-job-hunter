"""Outbound notifications (high-match alerts, daily digest, error reports) with delivery logging."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any, Protocol

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError

from app.bots.formatters import format_alert, format_digest, job_keyboard
from app.database.repositories import LogRepository, MatchRepository, ProfileRepository
from app.database.store import JsonStore
from app.logging import get_logger
from app.services.settings_service import SettingsService
from app.services.stats_service import StatsService
from app.utils.text import truncate

log = get_logger(__name__)

ERROR_THROTTLE = timedelta(minutes=30)
MAX_ALERTS_PER_RUN = 15


class MessageSender(Protocol):
    async def send(self, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None: ...


class TelegramSender:
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    async def send(self, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
        await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


class NotificationService:
    def __init__(
        self, db: JsonStore, sender: MessageSender | None, chat_ids: list[int], settings_service: SettingsService
    ) -> None:
        self.db = db
        self.sender = sender
        self.chat_ids = chat_ids
        self.prefs = settings_service

    @property
    def enabled(self) -> bool:
        return self.sender is not None and bool(self.chat_ids)

    async def _deliver(
        self, kind: str, text: str, markup: InlineKeyboardMarkup | None = None, job_id: int | None = None
    ) -> bool:
        ok_any = False
        for chat_id in self.chat_ids:
            status, error = "sent", None
            try:
                assert self.sender is not None
                await self.sender.send(chat_id, text, markup)
                ok_any = True
            except TelegramError as exc:
                status, error = "failed", truncate(f"{type(exc).__name__}: {exc}", 500)
                log.warning("telegram_send_failed", kind=kind, error=error)
            async with self.db.session() as session:
                await LogRepository(session).add_notification(
                    kind=kind, status=status, job_id=job_id, chat_id=str(chat_id), error=error
                )
        return ok_any

    async def send_high_match_alerts(self) -> int:
        """Alert on new matches above the threshold. Each match is alerted at most once (notified_at)."""
        if not self.enabled:
            return 0
        prefs = await self.prefs.get()
        if not prefs.alerts_enabled:
            return 0
        async with self.db.session() as session:
            ref = await ProfileRepository(session).active_ref()
            if ref is None:
                return 0
            pending = await MatchRepository(session).pending_alerts(ref, prefs.alert_threshold, MAX_ALERTS_PER_RUN)
        sent = 0
        for match in pending:
            delivered = await self._deliver(
                "alert", format_alert(match.job, match), job_keyboard(match.job), job_id=match.job_id
            )
            if delivered:
                sent += 1
                async with self.db.session() as session:
                    await MatchRepository(session).mark_notified(match.id)
        return sent

    async def send_daily_digest(self) -> bool:
        if not self.enabled:
            return False
        prefs = await self.prefs.get()
        if not prefs.digest_enabled:
            return False
        stats = await StatsService(self.db).overview(hours=24)
        async with self.db.session() as session:
            ref = await ProfileRepository(session).active_ref()
            matches = []
            if ref is not None:
                matches = await MatchRepository(session).recent_above(
                    ref, min_score=70, since=datetime.now(UTC) - timedelta(hours=24), limit=prefs.digest_size
                )
        return await self._deliver("digest", format_digest(matches, stats))

    async def send_error(self, title: str, detail: str) -> bool:
        """Error report to the owner, throttled so a persistently failing source doesn't spam."""
        if not self.enabled:
            return False
        async with self.db.session() as session:
            last = await LogRepository(session).last_notification("error")
        if last and last.sent_at > datetime.now(UTC) - ERROR_THROTTLE:
            return False
        text = f"⚠️ <b>{escape(title, quote=False)}</b>\n<pre>{escape(truncate(detail, 1500), quote=False)}</pre>"
        return await self._deliver("error", text)
