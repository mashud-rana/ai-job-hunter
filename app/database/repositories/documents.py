from __future__ import annotations

from app.database.models import GeneratedDocument
from app.database.store import JsonStore


class DocumentRepository:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    async def add(self, doc: GeneratedDocument) -> GeneratedDocument:
        return self.store.add("documents", doc)

    async def list_for_job(self, job_id: int) -> list[GeneratedDocument]:
        docs = [d for d in self.store.all("documents") if d.job_id == job_id]
        return sorted(docs, key=lambda d: (d.created_at, d.id), reverse=True)
