from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.database.models import CollectionLog, NotificationLog
from app.database.store import JsonStore


class LogRepository:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    async def add_collection(self, log: CollectionLog) -> CollectionLog:
        log.source = self.store.get("sources", log.source_id)
        return self.store.add("collection_logs", log)

    async def add_notification(
        self, *, kind: str, status: str, job_id: int | None = None, chat_id: str | None = None, error: str | None = None
    ) -> None:
        self.store.add(
            "notification_logs",
            NotificationLog(kind=kind, status=status, job_id=job_id, chat_id=chat_id, error=error or None),
        )

    async def last_notification(self, kind: str) -> NotificationLog | None:
        sent = [n for n in self.store.all("notification_logs") if n.kind == kind and n.status == "sent"]
        return max(sent, key=lambda n: n.sent_at, default=None)

    async def collection_summary(self, since: datetime) -> list[dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for lg in self.store.all("collection_logs"):
            if lg.started_at < since:
                continue
            key = lg.source.key if lg.source else str(lg.source_id)
            row = out.setdefault(
                key, {"source": key, "runs": 0, "fetched": 0, "new": 0, "duplicates": 0, "invalid": 0, "failures": 0}
            )
            row["runs"] += 1
            row["fetched"] += lg.fetched
            row["new"] += lg.new
            row["duplicates"] += lg.duplicates
            row["invalid"] += lg.invalid
            row["failures"] += 1 if lg.status == "failed" else 0
        return [out[k] for k in sorted(out)]

    async def purge_older_than(self, days: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        removed = 0
        for lg in [x for x in self.store.all("collection_logs") if x.started_at < cutoff]:
            self.store.delete("collection_logs", lg.id)
            removed += 1
        for n in [x for x in self.store.all("notification_logs") if x.sent_at < cutoff]:
            self.store.delete("notification_logs", n.id)
            removed += 1
        return removed
