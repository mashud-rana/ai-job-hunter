"""Candidate profile management: manual edits, CV upload, explicit confirmation of extracted data."""

from __future__ import annotations

import secrets
from pathlib import Path

from app.ai.cv_parser import CVError, extract_pdf_text, parse_cv
from app.ai.providers import LLMProvider
from app.database.models import CandidateProfile
from app.database.repositories import ProfileRepository
from app.database.store import JsonStore
from app.logging import get_logger
from app.schemas.profile import ProfileExtraction, ProfileIn

log = get_logger(__name__)


class ProfileError(ValueError):
    pass


class ProfileService:
    def __init__(self, db: JsonStore, providers: list[LLMProvider], uploads_dir: Path) -> None:
        self.db = db
        self.providers = providers
        self.uploads_dir = uploads_dir

    async def get(self) -> CandidateProfile | None:
        async with self.db.session() as session:
            return await ProfileRepository(session).get_active()

    async def save_manual(self, data: ProfileIn) -> CandidateProfile:
        """Manual edits are authoritative and mark the profile as verified."""
        payload = data.model_dump(exclude={"skills"})
        skills = [s.model_dump() for s in data.skills]
        async with self.db.session() as session:
            repo = ProfileRepository(session)
            profile = await repo.get_active()
            if profile is None:
                return await repo.create(payload, skills, verified=True)
            return await repo.update(profile, payload, skills, verified=True)

    async def upload_cv(self, filename: str, data: bytes) -> tuple[CandidateProfile, ProfileExtraction]:
        """Extract a profile from a PDF CV.

        A verified profile is never overwritten here; the extraction is stored as pending_profile and
        applied only via confirm_pending(). If no profile exists yet, one is created unverified.
        """
        text = extract_pdf_text(data)  # validates type / size / pages
        extraction = await parse_cv(text, self.providers)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        stored = f"cv_{secrets.token_hex(12)}.pdf"  # never trust the client filename for paths
        (self.uploads_dir / stored).write_bytes(data)
        safe_display = Path(filename or "cv.pdf").name[:200]

        async with self.db.session() as session:
            repo = ProfileRepository(session)
            profile = await repo.get_active()
            if profile is None:
                profile = await repo.create(
                    _extraction_to_payload(extraction, fallback_name="Candidate"),
                    [s.model_dump() for s in extraction.skills],
                    verified=False,
                )
            else:
                profile.pending_profile = extraction.model_dump()
            profile.cv_text = text
            profile.cv_filename = safe_display
            await repo.save(profile)
        log.info("cv_processed", method=extraction.method, skills=len(extraction.skills))
        return profile, extraction

    async def confirm_pending(self, merge_skills: bool = True) -> CandidateProfile:
        async with self.db.session() as session:
            repo = ProfileRepository(session)
            profile = await repo.get_active()
            if profile is None or not profile.pending_profile:
                raise ProfileError("no pending CV extraction to confirm")
            extraction = ProfileExtraction.model_validate(profile.pending_profile)
            payload = _extraction_to_payload(extraction, fallback_name=profile.name)
            # keep manually curated values when the CV didn't provide one
            payload = {k: v for k, v in payload.items() if v not in (None, [], "")}
            skills = [s.model_dump() for s in extraction.skills]
            if merge_skills:
                existing = [
                    {"name": s.name, "category": s.category, "years": s.years, "level": s.level} for s in profile.skills
                ]
                skills = existing + skills  # repo dedupes by normalized name (existing wins)
            profile.pending_profile = None
            return await repo.update(profile, payload, skills, verified=True)

    async def discard_pending(self) -> CandidateProfile:
        async with self.db.session() as session:
            profile = await ProfileRepository(session).get_active()
            if profile is None:
                raise ProfileError("no profile")
            profile.pending_profile = None
            return await ProfileRepository(session).save(profile)


def _extraction_to_payload(e: ProfileExtraction, fallback_name: str) -> dict:
    return {
        "name": e.name or fallback_name,
        "headline": e.headline,
        "years_experience": e.years_experience or 0,
        "target_titles": e.target_titles,
        "preferred_locations": e.preferred_locations,
        "summary": e.summary,
    }


__all__ = ["CVError", "ProfileError", "ProfileService"]
