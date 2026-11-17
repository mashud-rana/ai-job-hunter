from __future__ import annotations

from app.ai.cover_letter import generate_document
from app.ai.providers import LLMProvider
from app.database.models import GeneratedDocument
from app.database.repositories import DocumentRepository, JobRepository, ProfileRepository
from app.database.store import JsonStore
from app.services.matching_service import job_to_data, profile_to_data


class NotFoundError(LookupError):
    pass


class DocumentService:
    def __init__(self, db: JsonStore, providers: list[LLMProvider]) -> None:
        self.db = db
        self.providers = providers

    async def generate(self, job_id: int, kind: str) -> GeneratedDocument:
        async with self.db.session() as session:
            job = await JobRepository(session).get(job_id)
            if job is None:
                raise NotFoundError("job not found")
            profile = await ProfileRepository(session).get_active()
            if profile is None:
                raise NotFoundError("no candidate profile; create one first")
            jd, pd, profile_id = job_to_data(job), profile_to_data(profile), profile.id

        result = await generate_document(kind, jd, pd, self.providers)  # store lock not held during the AI call

        async with self.db.session() as session:
            doc = GeneratedDocument(
                job_id=job_id,
                profile_id=profile_id,
                kind=kind,
                content=result.content,
                provider=result.provider,
                model=result.model,
            )
            return await DocumentRepository(session).add(doc)

    async def list_for_job(self, job_id: int) -> list[GeneratedDocument]:
        async with self.db.session() as session:
            return await DocumentRepository(session).list_for_job(job_id)
