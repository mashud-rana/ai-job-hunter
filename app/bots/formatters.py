"""Telegram message formatting (HTML parse mode; every dynamic value is escaped)."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.database.models import CandidateProfile, Job, JobApplication, JobMatch
from app.schemas.match import CATEGORY_LABELS
from app.utils.text import truncate
from app.utils.urls import is_valid_public_url

MAX_MESSAGE = 4000  # Telegram limit is 4096
SOURCE_LABELS = {
    "remoteok": "Remote OK",
    "remotive": "Remotive",
    "arbeitnow": "Arbeitnow",
    "adzuna": "Adzuna",
    "linkedin": "LinkedIn",
    "bdjobs": "BDJobs",
    "bdjobstoday": "BDJobsToday",
    "careers": "Company careers",
}
SCORE_EMOJI = {"excellent": "🟢", "strong": "🟢", "good": "🟡", "moderate": "🟠", "poor": "🔴"}


def e(value: Any) -> str:
    return escape(str(value), quote=False) if value is not None else ""


def _date(dt: datetime | None) -> str:
    return dt.strftime("%d %b %Y") if dt else "n/a"


def source_label(job: Job) -> str:
    key = job.source.key if job.source is not None else ""
    return SOURCE_LABELS.get(key, key)


def job_headline(job: Job, match: JobMatch | None) -> str:
    score = ""
    if match is not None:
        score = f"{SCORE_EMOJI.get(match.category, '')} <b>{match.match_score}</b> · "
    company = f" — {e(job.company)}" if job.company else ""
    return f"{score}<b>{e(job.title)}</b>{company}"


def format_job_card(job: Job, match: JobMatch | None, detailed: bool = False) -> str:
    lines = [job_headline(job, match)]
    meta = [x for x in (job.location, job.remote_type if job.remote_type != "unknown" else None, job.job_type) if x]
    if meta:
        lines.append("📍 " + e(" · ".join(meta)))
    if job.salary_text:
        lines.append("💰 " + e(job.salary_text))
    lines.append(f"🗓 {_date(job.posted_at or job.collected_at)} · via {e(source_label(job))}")
    if match is not None:
        lines.append(f"🎯 {CATEGORY_LABELS.get(match.category, match.category)} ({match.match_score}/100)")
        if match.matched_skills:
            lines.append("✅ " + e(", ".join(match.matched_skills[:12])))
        if match.missing_skills:
            lines.append("➖ " + e(", ".join(match.missing_skills[:8])))
        if detailed and match.ai_reason:
            lines.append("💡 " + e(truncate(match.ai_reason, 400)))
    if detailed and job.description:
        lines.append("")
        lines.append(e(truncate(job.description, 1500)))
    return truncate("\n".join(lines), MAX_MESSAGE)


def job_keyboard(job: Job, include_details: bool = True) -> InlineKeyboardMarkup:
    row1 = []
    if include_details:
        row1.append(InlineKeyboardButton("📄 Details", callback_data=f"job:{job.id}"))
    if is_valid_public_url(job.apply_url):
        row1.append(InlineKeyboardButton("🔗 Apply", url=job.apply_url))
    row2 = [
        InlineKeyboardButton("⭐ Save", callback_data=f"save:{job.id}"),
        InlineKeyboardButton("✅ Applied", callback_data=f"applied:{job.id}"),
        InlineKeyboardButton("✉️ Cover letter", callback_data=f"cover:{job.id}"),
    ]
    return InlineKeyboardMarkup([row1, row2])


def format_job_list(title: str, rows: list[tuple[Job, JobMatch | None]], page: int, pages: int) -> str:
    if not rows:
        return f"<b>{e(title)}</b>\n\nNo jobs found yet."
    parts = [f"<b>{e(title)}</b> (page {page}/{pages})", ""]
    for i, (job, match) in enumerate(rows, 1):
        parts.append(f"{i}. {job_headline(job, match)}")
        loc = " · ".join(x for x in (job.location, source_label(job)) if x)
        if loc:
            parts.append(f"    {e(loc)}")
    return truncate("\n".join(parts), MAX_MESSAGE)


def list_keyboard(rows: list[tuple[Job, JobMatch | None]], kind: str, page: int, pages: int) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"{i}. {truncate(job.title, 40)}", callback_data=f"job:{job.id}")]
        for i, (job, _) in enumerate(rows, 1)
    ]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"page:{kind}:{page - 1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"page:{kind}:{page + 1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


def format_alert(job: Job, match: JobMatch) -> str:
    return "🚨 <b>High match alert</b>\n\n" + format_job_card(job, match)


def format_digest(matches: list[JobMatch], stats: dict[str, Any]) -> str:
    jobs = stats.get("jobs", {})
    lines = [
        "☀️ <b>Daily job digest</b>",
        f"New jobs (24h): <b>{jobs.get('new_since', 0)}</b> · Active: {jobs.get('active_jobs', 0)}",
        "",
    ]
    if not matches:
        lines.append("No new strong matches in the last 24 hours.")
    for i, m in enumerate(matches, 1):
        lines.append(f"{i}. {job_headline(m.job, m)}")
        extra = " · ".join(x for x in (m.job.location, source_label(m.job)) if x)
        if extra:
            lines.append(f"    {e(extra)}")
    failed = [c["source"] for c in stats.get("collection", []) if c.get("failures")]
    if failed:
        lines += ["", f"⚠️ Sources with errors: {e(', '.join(failed))}"]
    return truncate("\n".join(lines), MAX_MESSAGE)


def format_stats(stats: dict[str, Any]) -> str:
    jobs = stats["jobs"]
    lines = [
        "📊 <b>Statistics</b>",
        f"Jobs: {jobs['total_jobs']} total · {jobs['active_jobs']} active · {jobs.get('new_since', 0)} new (24h)",
    ]
    if jobs.get("by_source"):
        lines.append("By source: " + e(", ".join(f"{k}: {v}" for k, v in jobs["by_source"].items())))
    if stats.get("matches_by_category"):
        order = ["excellent", "strong", "good", "moderate", "poor"]
        dist = stats["matches_by_category"]
        lines.append("Matches: " + ", ".join(f"{CATEGORY_LABELS[c]} {dist[c]}" for c in order if c in dist))
    if stats.get("applications_by_status"):
        lines.append("Applications: " + e(", ".join(f"{k} {v}" for k, v in stats["applications_by_status"].items())))
    if stats.get("collection"):
        lines.append("")
        lines.append("<b>Collection (24h)</b>")
        for c in stats["collection"]:
            warn = f" ⚠️ {c['failures']} failed" if c["failures"] else ""
            lines.append(e(f"{c['source']}: {c['runs']} runs, {c['fetched']} fetched, {c['new']} new") + warn)
    return truncate("\n".join(lines), MAX_MESSAGE)


def format_profile(p: CandidateProfile | None) -> str:
    if p is None:
        return "No candidate profile yet. Upload a CV via the dashboard or PUT /api/profile."
    by_cat: dict[str, list[str]] = {}
    for s in p.skills:
        by_cat.setdefault(s.category or "other", []).append(s.name)
    lines = [
        f"👤 <b>{e(p.name)}</b>" + (" ✔️" if p.is_verified else " (unverified)"),
        e(p.headline or ""),
        f"Experience: {p.years_experience:g} years",
        "Targets: " + e(", ".join(p.target_titles)),
        "Locations: " + e(", ".join(p.preferred_locations)),
        "Types: " + e(", ".join(p.job_types)),
        "",
    ]
    for cat, names in sorted(by_cat.items()):
        lines.append(f"<b>{e(cat.title())}</b>: {e(', '.join(names))}")
    if p.pending_profile:
        lines += ["", "📝 A CV extraction is waiting for confirmation in the dashboard."]
    return truncate("\n".join(x for x in lines if x is not None), MAX_MESSAGE)


def format_applications(title: str, apps: list[JobApplication]) -> str:
    if not apps:
        return f"<b>{e(title)}</b>\n\nNothing here yet."
    lines = [f"<b>{e(title)}</b>", ""]
    for a in apps:
        when = f" · {_date(a.applied_at)}" if a.applied_at else ""
        follow = f" · follow up {_date(a.follow_up_at)}" if a.follow_up_at else ""
        lines.append(f"• <b>{e(a.position)}</b> — {e(a.company or '')} [{e(a.status)}]{when}{follow}")
    return truncate("\n".join(lines), MAX_MESSAGE)
