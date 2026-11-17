"""Telegram bot (python-telegram-bot v22, long polling inside the app's event loop).

Only chats listed in TELEGRAM_ALLOWED_CHAT_IDS can use it; anyone else is ignored. To find your chat
id, message the bot and call /start: unauthorised chats are told their id.
"""

from __future__ import annotations

import contextlib

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, filters

from app.ai.cover_letter import GenerationUnavailable
from app.bots import formatters as fmt
from app.container import Container
from app.database.repositories import JobFilters, JobRepository, ProfileRepository
from app.logging import get_logger
from app.services.application_service import ApplicationNotFound
from app.services.document_service import NotFoundError
from app.utils.text import truncate

log = get_logger(__name__)

PAGE_SIZE = 8
HELP = """<b>AI Job Hunter</b>
/jobs – latest matched jobs
/topjobs – highest-scoring jobs
/stats – collection & match statistics
/saved – saved jobs
/applied – applied jobs
/profile – your candidate profile
/settings – notification preferences
/help – this message"""


class JobHunterBot:
    def __init__(self, container: Container) -> None:
        self.c = container
        s = container.settings
        self.allowed = filters.Chat(chat_id=s.telegram_allowed_chat_ids)
        self.app: Application = Application.builder().token(s.telegram_bot_token).build()
        self._register()

    # ------------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return bool(self.app.running)

    async def start(self) -> None:
        await self.app.initialize()
        await self.app.bot.set_my_commands(
            [
                ("jobs", "Latest matched jobs"),
                ("topjobs", "Highest-scoring jobs"),
                ("stats", "Statistics"),
                ("saved", "Saved jobs"),
                ("applied", "Applied jobs"),
                ("profile", "Candidate profile"),
                ("settings", "Notification preferences"),
                ("help", "Show commands"),
            ]
        )
        await self.app.start()
        assert self.app.updater is not None
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram_bot_started")

    async def stop(self) -> None:
        try:
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            if self.app.running:
                await self.app.stop()
            await self.app.shutdown()
        except Exception:
            log.exception("telegram_bot_stop_failed")

    # ------------------------------------------------------------------ wiring
    def _register(self) -> None:
        a = self.app
        a.add_handler(CommandHandler("start", self.cmd_start))
        commands = {
            "help": self.cmd_help,
            "jobs": self.cmd_jobs,
            "topjobs": self.cmd_topjobs,
            "stats": self.cmd_stats,
            "saved": self.cmd_saved,
            "applied": self.cmd_applied,
            "profile": self.cmd_profile,
            "settings": self.cmd_settings,
        }
        for name, handler in commands.items():
            a.add_handler(CommandHandler(name, handler, filters=self.allowed))
        a.add_handler(CallbackQueryHandler(self.on_callback))
        a.add_error_handler(self.on_error)

    def _authorized(self, update: Update) -> bool:
        chat = update.effective_chat
        return chat is not None and chat.id in self.c.settings.telegram_allowed_chat_ids

    async def _reply(self, update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        assert update.effective_message is not None
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
        )

    # ------------------------------------------------------------------ commands
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            chat_id = update.effective_chat.id if update.effective_chat else "?"
            await self._reply(
                update,
                f"This bot is private. Your chat id is <code>{chat_id}</code>; add it to "
                "TELEGRAM_ALLOWED_CHAT_IDS to get access.",
            )
            log.info("telegram_unauthorized_start", chat_id=chat_id)
            return
        await self._reply(update, "👋 Hi! I'll send you high-match job alerts and a daily digest.\n\n" + HELP)

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reply(update, HELP)

    async def cmd_jobs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text, markup = await self._job_list("jobs", 1)
        await self._reply(update, text, markup)

    async def cmd_topjobs(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text, markup = await self._job_list("top", 1)
        await self._reply(update, text, markup)

    async def cmd_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        stats = await self.c.services.stats.overview(24)
        await self._reply(update, fmt.format_stats(stats))

    async def cmd_saved(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        page = await self.c.services.applications.list_applications(["saved", "interested"], 1, 20)
        await self._reply(update, fmt.format_applications("Saved jobs", page.items))

    async def cmd_applied(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        page = await self.c.services.applications.list_applications(["applied", "interview", "offer"], 1, 20)
        await self._reply(update, fmt.format_applications("Applied jobs", page.items))

    async def cmd_profile(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        profile = await self.c.services.profiles.get()
        await self._reply(update, fmt.format_profile(profile))

    async def cmd_settings(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text, markup = await self._settings_view()
        await self._reply(update, text, markup)

    # ------------------------------------------------------------------ views
    async def _job_list(self, kind: str, page: int) -> tuple[str, InlineKeyboardMarkup | None]:
        async with self.c.db.session() as session:
            ref = await ProfileRepository(session).active_ref()
            if kind == "top":
                filters_ = JobFilters(sort="score", min_score=50)
                title = "Top matched jobs"
            else:
                filters_ = JobFilters(sort="collected_at", min_score=50 if ref else None)
                title = "Latest matched jobs"
            result = await JobRepository(session).list_jobs(filters_, ref, page=page, per_page=PAGE_SIZE)
        text = fmt.format_job_list(title, result.items, result.page, result.pages)
        markup = fmt.list_keyboard(result.items, kind, result.page, result.pages) if result.items else None
        return text, markup

    async def _job_detail(self, job_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
        async with self.c.db.session() as session:
            job = await JobRepository(session).get(job_id)
            if job is None:
                return "Job not found.", None
            ref = await ProfileRepository(session).active_ref()
            match = await JobRepository(session).get_match(job_id, ref)
        return fmt.format_job_card(job, match, detailed=True), fmt.job_keyboard(job, include_details=False)

    async def _settings_view(self) -> tuple[str, InlineKeyboardMarkup]:
        p = await self.c.services.settings.get()
        text = (
            "⚙️ <b>Notification settings</b>\n"
            f"High-match alerts: <b>{'on' if p.alerts_enabled else 'off'}</b>\n"
            f"Alert threshold: <b>{p.alert_threshold}</b>\n"
            f"Daily digest (9:00 Asia/Dhaka): <b>{'on' if p.digest_enabled else 'off'}</b>"
        )
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"Alerts: {'turn off' if p.alerts_enabled else 'turn on'}", callback_data="set:alerts"
                    )
                ],
                [
                    InlineKeyboardButton(
                        f"Digest: {'turn off' if p.digest_enabled else 'turn on'}", callback_data="set:digest"
                    )
                ],
                [
                    InlineKeyboardButton(f"{'• ' if p.alert_threshold == t else ''}{t}", callback_data=f"set:th:{t}")
                    for t in (70, 75, 80, 85, 90)
                ],
            ]
        )
        return text, markup

    # ------------------------------------------------------------------ callbacks
    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._authorized(update):
            await query.answer("Not authorised", show_alert=True)
            return
        data = query.data or ""
        try:
            await self._handle_callback(update, data)
        except (NotFoundError, ApplicationNotFound) as exc:
            await query.answer(str(exc), show_alert=True)
        except ValueError:
            await query.answer("Invalid action", show_alert=True)

    async def _handle_callback(self, update: Update, data: str) -> None:
        query = update.callback_query
        assert query is not None
        action, _, arg = data.partition(":")
        if action == "job":
            await query.answer()
            text, markup = await self._job_detail(int(arg))
            await self._reply(update, text, markup)
        elif action == "save":
            await self.c.services.applications.set_status_for_job(int(arg), "saved")
            await query.answer("⭐ Saved")
        elif action == "applied":
            await self.c.services.applications.set_status_for_job(int(arg), "applied")
            await query.answer("✅ Marked as applied")
        elif action == "cover":
            await query.answer("Generating cover letter…")
            try:
                doc = await self.c.services.documents.generate(int(arg), "cover_letter")
            except GenerationUnavailable as exc:
                await self._reply(update, f"Cover letter unavailable: {fmt.e(exc)}")
                return
            await self._reply(
                update, "<b>Cover letter (draft — review before sending)</b>\n\n" + fmt.e(truncate(doc.content, 3800))
            )
        elif action == "page":
            kind, _, page = arg.partition(":")
            await query.answer()
            text, markup = await self._job_list(kind if kind in ("jobs", "top") else "jobs", max(1, int(page)))
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
            )
        elif action == "set":
            prefs = await self.c.services.settings.get()
            if arg == "alerts":
                await self.c.services.settings.update(alerts_enabled=not prefs.alerts_enabled)
            elif arg == "digest":
                await self.c.services.settings.update(digest_enabled=not prefs.digest_enabled)
            elif arg.startswith("th:"):
                await self.c.services.settings.update(alert_threshold=int(arg[3:]))
            else:
                raise ValueError(arg)
            await query.answer("Saved")
            text, markup = await self._settings_view()
            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        else:
            raise ValueError(action)

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("telegram_handler_error", error=str(ctx.error)[:500])
        if isinstance(update, Update) and update.effective_message and self._authorized(update):
            with contextlib.suppress(Exception):
                await update.effective_message.reply_text("Sorry, something went wrong. It has been logged.")


def build_bot(container: Container) -> JobHunterBot | None:
    if not container.settings.telegram_enabled:
        log.warning("telegram_disabled", reason="TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_CHAT_IDS not set")
        return None
    return JobHunterBot(container)


__all__ = ["JobHunterBot", "build_bot"]
