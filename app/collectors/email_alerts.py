"""Job-alert emails read over IMAP (read-only).

Used for sources with no public API whose alert emails the user subscribes to themselves
(LinkedIn Job Alerts, BDJobs alerts). The mailbox is opened with readonly=True: nothing is
deleted, moved or marked as read.
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import imaplib
from abc import abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from app.collectors.base import Availability, BaseCollector
from app.logging import get_logger
from app.schemas.job import RawJob

log = get_logger(__name__)

MAX_MESSAGES = 200


@dataclass
class AlertEmail:
    message_id: str
    subject: str
    sender: str
    received_at: datetime | None
    html: str
    text: str


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _parse_message(raw: bytes) -> AlertEmail:
    msg: Message = email.message_from_bytes(raw)
    html_parts: list[str] = []
    text_parts: list[str] = []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            body = payload.decode(charset, errors="replace")
        except LookupError:
            body = payload.decode("utf-8", errors="replace")
        if ctype == "text/html":
            html_parts.append(body)
        elif ctype == "text/plain":
            text_parts.append(body)
    received = None
    if msg.get("Date"):
        try:
            received = parsedate_to_datetime(msg["Date"])
        except (TypeError, ValueError):
            received = None
    return AlertEmail(
        message_id=(msg.get("Message-ID") or "").strip(),
        subject=_decode(msg.get("Subject")),
        sender=_decode(msg.get("From")),
        received_at=received,
        html="\n".join(html_parts),
        text="\n".join(text_parts),
    )


class EmailAlertCollector(BaseCollector):
    targeted = True  # alerts are already personalised by the user's alert settings

    @property
    @abstractmethod
    def senders(self) -> list[str]: ...

    @abstractmethod
    def parse_email(self, alert: AlertEmail) -> list[RawJob]: ...

    def availability(self) -> Availability:
        s = self.settings
        if not (s.imap_host and s.imap_user and s.imap_password):
            return Availability.missing("IMAP_HOST / IMAP_USER / IMAP_PASSWORD not configured")
        if not self.senders:
            return Availability.missing("no alert sender addresses configured")
        return Availability.ok()

    async def collect(self) -> list[RawJob]:
        emails = await asyncio.wait_for(asyncio.to_thread(self._fetch_emails), timeout=self.settings.collector_timeout)
        jobs: dict[str, RawJob] = {}
        for alert in emails:
            try:
                for job in self.parse_email(alert):
                    jobs.setdefault(job.external_id, job)
            except Exception as exc:  # one malformed email must not drop the rest
                log.warning("alert_email_parse_failed", collector=self.key, error=str(exc))
        return list(jobs.values())

    def _fetch_emails(self) -> list[AlertEmail]:
        s = self.settings
        since = (datetime.now(UTC) - timedelta(days=s.imap_lookback_days)).strftime("%d-%b-%Y")
        conn = imaplib.IMAP4_SSL(s.imap_host, s.imap_port, timeout=int(s.http_timeout) or 30)
        try:
            conn.login(s.imap_user, s.imap_password)
            status, _ = conn.select(_quote(s.imap_folder), readonly=True)
            if status != "OK":
                raise RuntimeError(f"cannot open IMAP folder {s.imap_folder!r}")
            ids: list[bytes] = []
            for sender in self.senders:
                status, data = conn.search(None, "SINCE", since, "FROM", _quote(sender))
                if status == "OK" and data and data[0]:
                    ids.extend(data[0].split())
            unique_ids = sorted(set(ids), key=int)[-MAX_MESSAGES:]
            result = []
            for msg_id in unique_ids:
                # BODY.PEEK keeps the \Seen flag untouched
                status, parts = conn.fetch(msg_id.decode(), "(BODY.PEEK[])")
                if status != "OK":
                    continue
                for part in parts:
                    if isinstance(part, tuple) and isinstance(part[1], bytes):
                        result.append(_parse_message(part[1]))
            return result
        finally:
            with contextlib.suppress(Exception):
                conn.logout()


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
