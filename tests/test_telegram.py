from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.bots import formatters as fmt
from app.bots.telegram_bot import JobHunterBot, build_bot
from app.database.models import Job, JobMatch, JobSource
from app.database.repositories import JobRepository, ProfileRepository, SourceRepository
from app.schemas.job import RawJob
from app.utils.normalize import normalize_job
from tests.fakes import FakeSender


async def seed(container, scores=(92, 85, 60)):
    async with container.db.session() as s:
        src = await SourceRepository(s).upsert("remoteok", "Remote OK", True, None)
        await ProfileRepository(s).create(
            {"name": "Md. Test", "years_experience": 5, "target_titles": ["Senior Laravel Developer"]},
            [{"name": "PHP", "category": "backend"}, {"name": "Laravel", "category": "backend"}],
            True,
        )
        ref = await ProfileRepository(s).active_ref()
        ids = []
        for i, score in enumerate(scores):
            raw = RawJob(
                source="remoteok",
                external_id=str(i),
                title=f"Senior <Laravel> Dev {i}",
                company="A & B",
                location="Remote",
                apply_url=f"https://remoteok.com/remote-jobs/{i}",
                description="PHP",
            )
            job = await JobRepository(s).insert_if_new(normalize_job(raw), src.id)
            s.add(
                JobMatch(
                    job_id=job.id,
                    profile_id=ref.id,
                    profile_version=ref.version,
                    match_score=score,
                    category="excellent" if score >= 90 else "strong" if score >= 80 else "moderate",
                    matched_skills=["PHP"],
                    missing_skills=["Kubernetes"],
                    recommendation="apply",
                    provider="rules",
                    confidence=0.5,
                    ai_reason="ok <b>",
                )
            )
            ids.append(job.id)
        return ids


def test_formatters_escape_html():
    src = JobSource(key="remoteok", name="Remote OK")
    job = Job(
        id=1,
        title="Dev <script>",
        company="A & B",
        location="Remote",
        remote_type="remote",
        apply_url="https://x.example.com/1",
        description="<b>desc</b>",
        tags=[],
        collected_at=datetime.now(UTC),
    )
    job.source = src
    m = JobMatch(
        match_score=91,
        category="excellent",
        matched_skills=["PHP"],
        missing_skills=[],
        ai_reason="because <i>",
        recommendation="apply",
    )
    card = fmt.format_job_card(job, m, detailed=True)
    assert "Dev &lt;script&gt;" in card and "A &amp; B" in card and "&lt;b&gt;desc" in card
    assert "via Remote OK" in card and "Excellent Match" in card
    kb = fmt.job_keyboard(job)
    buttons = [b for row in kb.inline_keyboard for b in row]
    assert any(b.url == "https://x.example.com/1" for b in buttons)
    assert {b.callback_data for b in buttons if b.callback_data} == {"job:1", "save:1", "applied:1", "cover:1"}
    # unsafe apply URL never becomes a button
    job.apply_url = "javascript:alert(1)"
    assert not any(b.url for row in fmt.job_keyboard(job).inline_keyboard for b in row)


async def test_alerts_sent_once_and_logged(container, sender):
    await seed(container)
    notifications = container.services.notifications
    assert await notifications.send_high_match_alerts() == 2  # 92 and 85 >= 80
    assert len(sender.messages) == 2 and all(m["chat_id"] == 111 for m in sender.messages)
    assert "High match alert" in sender.messages[0]["text"]
    assert await notifications.send_high_match_alerts() == 0  # never re-alerted
    async with container.db.session() as s:
        logs = s.all("notification_logs")
        assert [lg.status for lg in logs] == ["sent", "sent"]
        assert all(m.notified_at for m in s.all("matches") if m.match_score >= 80)


async def test_alert_threshold_and_disable_from_settings(container, sender):
    await seed(container)
    await container.services.settings.update(alert_threshold=90)
    assert await container.services.notifications.send_high_match_alerts() == 1
    await container.services.settings.update(alerts_enabled=False)
    assert await container.services.notifications.send_high_match_alerts() == 0


async def test_failed_delivery_is_logged_and_retried_later(container):
    await seed(container)
    container.services.notifications.sender = FakeSender(fail=True)
    assert await container.services.notifications.send_high_match_alerts() == 0
    async with container.db.session() as s:
        assert {lg.status for lg in s.all("notification_logs")} == {"failed"}
    container.services.notifications.sender = ok = FakeSender()
    assert await container.services.notifications.send_high_match_alerts() == 2 and len(ok.messages) == 2


async def test_digest_and_error_throttle(container, sender):
    await seed(container)
    assert await container.services.notifications.send_daily_digest()
    digest = sender.messages[-1]["text"]
    assert "Daily job digest" in digest and "Senior &lt;Laravel&gt; Dev 0" in digest
    assert await container.services.notifications.send_error("Collector failed", "remotive: <timeout>")
    assert "&lt;timeout&gt;" in sender.messages[-1]["text"]
    assert not await container.services.notifications.send_error("Again", "x")  # throttled


async def test_notifications_disabled_without_telegram(container):
    container.services.notifications.sender = None
    assert await container.services.notifications.send_high_match_alerts() == 0
    assert not await container.services.notifications.send_daily_digest()


def test_build_bot_requires_config(container):
    container.settings.telegram_bot_token = ""
    assert build_bot(container) is None


def _update(chat_id: int, data: str | None = None):
    message = SimpleNamespace(reply_text=AsyncMock())
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock()) if data else None
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), effective_message=message, callback_query=query)


async def test_bot_commands_and_callbacks(container):
    container.settings.telegram_bot_token = "123456:TEST-token-not-real"
    ids = await seed(container)
    bot = JobHunterBot(container)  # builds offline; nothing is started

    up = _update(999)
    await bot.cmd_start(up, None)
    assert "private" in up.effective_message.reply_text.call_args.args[0]

    up = _update(111)
    await bot.cmd_topjobs(up, None)
    text = up.effective_message.reply_text.call_args.args[0]
    assert "Top matched jobs" in text and text.index("92") < text.index("85")

    for cmd in (bot.cmd_jobs, bot.cmd_stats, bot.cmd_profile, bot.cmd_settings, bot.cmd_help):
        up = _update(111)
        await cmd(up, None)
        assert up.effective_message.reply_text.await_count == 1

    up = _update(111, f"job:{ids[0]}")
    await bot.on_callback(up, None)
    assert "Kubernetes" in up.effective_message.reply_text.call_args.args[0]

    await bot.on_callback(_update(111, f"save:{ids[0]}"), None)
    await bot.on_callback(_update(111, f"applied:{ids[1]}"), None)
    up = _update(111)
    await bot.cmd_saved(up, None)
    assert "Dev 0" in up.effective_message.reply_text.call_args.args[0]
    up = _update(111)
    await bot.cmd_applied(up, None)
    assert "Dev 1" in up.effective_message.reply_text.call_args.args[0]

    up = _update(111, f"cover:{ids[0]}")  # no AI provider configured
    await bot.on_callback(up, None)
    assert "unavailable" in up.effective_message.reply_text.call_args.args[0]

    up = _update(111, "set:th:85")
    await bot.on_callback(up, None)
    assert (await container.services.settings.get()).alert_threshold == 85
    up.callback_query.edit_message_text.assert_awaited()

    up = _update(111, "bogus:1")
    await bot.on_callback(up, None)
    assert up.callback_query.answer.call_args.kwargs.get("show_alert")

    up = _update(999, f"save:{ids[0]}")  # unauthorised callback ignored
    await bot.on_callback(up, None)
    assert up.callback_query.answer.call_args.args[0] == "Not authorised"

    up = _update(111, "job:999999")
    await bot.on_callback(up, None)
    assert "not found" in up.effective_message.reply_text.call_args.args[0]


async def test_pending_alert_window(container, sender):
    """Matches created more than 24h ago don't show up in the digest list."""
    await seed(container)
    async with container.db.session() as s:
        for m in s.all("matches"):
            m.created_at = datetime.now(UTC) - timedelta(days=3)
    await container.services.notifications.send_daily_digest()
    assert "No new strong matches" in sender.messages[-1]["text"]
