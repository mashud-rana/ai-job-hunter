"""In-memory store persisted as JSON files. Replaces the database.

All records live in memory (a personal job hunter holds a few thousand at most) and are written to
JSON after changes. Access goes through ``async with store.session() as s:`` (an asyncio lock) and
the repositories, exactly like before, so services don't care where data lives.

Files: sources.json, jobs.json, matches.json, profiles.json, applications.json, documents.json,
notification_logs.json, collection_logs.json, state.json (settings, id counters, seen-job keys).
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import Settings
from app.database.backends import GistBackend, LocalBackend, StorageBackend, StorageError
from app.database.models import (
    CandidateProfile,
    CollectionLog,
    GeneratedDocument,
    Job,
    JobApplication,
    JobMatch,
    JobSource,
    NotificationLog,
    Record,
)
from app.logging import get_logger

log = get_logger(__name__)

COLLECTIONS: dict[str, type[Record]] = {
    "sources": JobSource,
    "jobs": Job,
    "matches": JobMatch,
    "profiles": CandidateProfile,
    "applications": JobApplication,
    "documents": GeneratedDocument,
    "notification_logs": NotificationLog,
    "collection_logs": CollectionLog,
}
TABLE_FOR: dict[type, str] = {cls: name for name, cls in COLLECTIONS.items()}
LOG_CAPS = {"notification_logs": 1000, "collection_logs": 1000}
SEEN_RETENTION_DAYS = 120

_held: contextvars.ContextVar[bool] = contextvars.ContextVar("store_lock_held", default=False)


def build_backend(settings: Settings) -> StorageBackend:
    if settings.storage_backend == "gist":
        return GistBackend(settings.gist_id, settings.github_token)
    return LocalBackend(settings.data_dir)


class JsonStore:
    def __init__(self, settings: Settings, backend: StorageBackend | None = None) -> None:
        self.settings = settings
        self.backend = backend or build_backend(settings)
        self.tables: dict[str, dict[int, Any]] = {name: {} for name in COLLECTIONS}
        self.state: dict[str, Any] = {"counters": {}, "settings": {}, "seen": {}}
        self._dirty: set[str] = set()
        self._hashes: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._flush_lock = asyncio.Lock()  # writes happen one at a time, in order
        self._last_write = 0.0
        self.loaded = False
        self.last_error: str | None = None

    # ------------------------------------------------------------------ lifecycle
    async def load(self) -> None:
        files = await self.backend.read_all()
        for name, cls in COLLECTIONS.items():
            text = files.get(f"{name}.json")
            rows = json.loads(text) if text else []
            self.tables[name] = {r["id"]: cls.from_dict(r) for r in rows if r.get("id") is not None}
            self._hashes[name] = _digest(text or "")
        if files.get("state.json"):
            self.state.update(json.loads(files["state.json"]))
        self._hashes["state"] = _digest(files.get("state.json") or "")
        self._link()
        self.loaded = True
        log.info(
            "store_loaded",
            backend=self.backend.name,
            **{k: len(v) for k, v in self.tables.items() if k in ("jobs", "matches", "applications")},
        )

    def _link(self) -> None:
        """Resolve object references after loading."""
        sources = self.tables["sources"]
        jobs = self.tables["jobs"]
        for job in jobs.values():
            job.source = sources.get(job.source_id)
        for match in self.tables["matches"].values():
            match.job = jobs.get(match.job_id)
            match.profile = self.tables["profiles"].get(match.profile_id)
        for app in self.tables["applications"].values():
            app.job = jobs.get(app.job_id) if app.job_id else None
        for clog in self.tables["collection_logs"].values():
            clog.source = sources.get(clog.source_id)

    async def close(self) -> None:
        try:
            await self.flush(force=True)
        finally:
            await self.backend.aclose()

    # Compatibility with the old Database API used by health checks / tests.
    async def dispose(self) -> None:
        await self.close()

    # ------------------------------------------------------------------ unit of work
    @asynccontextmanager
    async def session(self) -> AsyncIterator[JsonStore]:
        """Serialises access. Re-entrant within one task. Persists changes on exit."""
        if _held.get():
            yield self
            return
        async with self._lock:
            token = _held.set(True)
            try:
                yield self
            finally:
                _held.reset(token)
        if self._dirty:
            await self.flush()

    # ------------------------------------------------------------------ records
    def next_id(self, table: str) -> int:
        counters = self.state.setdefault("counters", {})
        current = max(counters.get(table, 0), max(self.tables[table], default=0))
        counters[table] = current + 1
        self.touch("state")
        return current + 1

    def add(self, table: str | Record, record: Any = None) -> Any:
        """add("jobs", job) or add(job): the collection is inferred from the record type."""
        if record is None:
            record, table = table, TABLE_FOR[type(table)]
        assert isinstance(table, str)
        self._link_one(record)
        if record.id is None:
            record.id = self.next_id(table)
        self.tables[table][record.id] = record
        self.touch(table)
        if table in LOG_CAPS and len(self.tables[table]) > LOG_CAPS[table]:
            for old in sorted(self.tables[table])[: len(self.tables[table]) - LOG_CAPS[table]]:
                del self.tables[table][old]
        return record

    def _link_one(self, r: Any) -> None:
        if isinstance(r, Job):
            r.source = self.tables["sources"].get(r.source_id)
        elif isinstance(r, JobMatch):
            r.job = self.tables["jobs"].get(r.job_id)
            r.profile = self.tables["profiles"].get(r.profile_id)
        elif isinstance(r, JobApplication):
            r.job = self.tables["jobs"].get(r.job_id) if r.job_id else None
        elif isinstance(r, CollectionLog):
            r.source = self.tables["sources"].get(r.source_id)

    def delete(self, table: str, record_id: int) -> Any | None:
        removed = self.tables[table].pop(record_id, None)
        if removed is not None:
            self.touch(table)
        return removed

    def touch(self, *tables: str) -> None:
        self._dirty.update(tables)

    def all(self, table: str) -> list[Any]:
        return list(self.tables[table].values())

    def get(self, table: str, record_id: int | None) -> Any | None:
        return self.tables[table].get(record_id) if record_id is not None else None

    # ------------------------------------------------------------------ seen keys (dedupe after pruning)
    def remember(self, *keys: str) -> None:
        seen = self.state.setdefault("seen", {})
        today = datetime.now(UTC).date().isoformat()
        for k in keys:
            seen[k] = today
        self.touch("state")

    def was_seen(self, *keys: str) -> bool:
        seen = self.state.get("seen", {})
        return any(k in seen for k in keys)

    def forget_old_seen(self) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=SEEN_RETENTION_DAYS)).date().isoformat()
        seen = self.state.get("seen", {})
        old = [k for k, d in seen.items() if d < cutoff]
        for k in old:
            del seen[k]
        if old:
            self.touch("state")
        return len(old)

    # ------------------------------------------------------------------ persistence
    def _serialize(self, name: str) -> str:
        if name == "state":
            return json.dumps(self.state, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        rows = [r.to_dict() for _, r in sorted(self.tables[name].items())]
        return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))

    async def flush(self, force: bool = False) -> bool:
        """Write changed collections. ``force`` also re-checks every collection (catches in-place edits)
        and bypasses the remote-backend debounce."""
        recent = time.monotonic() - self._last_write < self.settings.storage_flush_seconds
        if not force and self.backend.name == "gist" and recent:
            return False  # debounced; the next session/forced flush writes it
        async with self._flush_lock:
            return await self._flush(force)

    async def _flush(self, force: bool) -> bool:
        names = [*COLLECTIONS, "state"] if force else sorted(self._dirty)
        changed: dict[str, str] = {}
        for name in names:
            text = self._serialize(name)
            digest = _digest(text)
            if digest != self._hashes.get(name):
                changed[f"{name}.json"] = text
                self._hashes[name] = digest
        self._dirty.clear()
        if not changed:
            return False
        try:
            await self.backend.write(changed)
        except (StorageError, OSError) as exc:
            # keep data in memory and retry on the next flush
            for fname in changed:
                self._hashes.pop(fname[:-5], None)
                self._dirty.add(fname[:-5])
            self.last_error = str(exc)
            log.error("store_flush_failed", backend=self.backend.name, error=str(exc))
            return False
        self._last_write = time.monotonic()
        self.last_error = None
        log.debug("store_flushed", files=sorted(changed))
        return True


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()
