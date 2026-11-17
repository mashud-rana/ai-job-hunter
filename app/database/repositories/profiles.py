from __future__ import annotations

from typing import Any

from app.database.models import CandidateProfile, CandidateSkill, utcnow
from app.database.repositories.jobs import ProfileRef
from app.database.store import JsonStore

PROFILE_FIELDS = (
    "name",
    "headline",
    "years_experience",
    "target_titles",
    "preferred_locations",
    "job_types",
    "min_salary",
    "salary_currency",
    "summary",
)


def normalize_skill(name: str) -> str:
    return " ".join(name.lower().split())


class ProfileRepository:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    async def get_active(self) -> CandidateProfile | None:
        active = sorted((p for p in self.store.all("profiles") if p.is_active), key=lambda p: p.id)
        return active[0] if active else None

    async def active_ref(self) -> ProfileRef | None:
        p = await self.get_active()
        return ProfileRef(id=p.id, version=p.version) if p else None

    async def create(self, data: dict[str, Any], skills: list[dict[str, Any]], verified: bool) -> CandidateProfile:
        profile = CandidateProfile(**{k: v for k, v in data.items() if k in PROFILE_FIELDS}, is_verified=verified)
        self.store.add("profiles", profile)
        self._replace_skills(profile, skills)
        return profile

    async def update(
        self,
        profile: CandidateProfile,
        data: dict[str, Any],
        skills: list[dict[str, Any]] | None,
        verified: bool | None = None,
    ) -> CandidateProfile:
        for key, value in data.items():
            if key in PROFILE_FIELDS:
                setattr(profile, key, value)
        if skills is not None:
            self._replace_skills(profile, skills)
        if verified is not None:
            profile.is_verified = verified
        profile.version += 1  # invalidates matches for the previous profile version
        profile.updated_at = utcnow()
        self.store.touch("profiles")
        return profile

    async def save(self, profile: CandidateProfile) -> CandidateProfile:
        """Persist in-place edits (pending_profile, cv_text, ...) without bumping the version."""
        profile.updated_at = utcnow()
        self.store.touch("profiles")
        return profile

    def _replace_skills(self, profile: CandidateProfile, skills: list[dict[str, Any]]) -> None:
        seen: set[str] = set()
        out: list[CandidateSkill] = []
        for s in skills:
            name = str(s.get("name", "")).strip()
            norm = normalize_skill(name)
            if not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(
                CandidateSkill(
                    id=len(out) + 1,
                    profile_id=profile.id,
                    name=name[:100],
                    normalized_name=norm[:100],
                    category=s.get("category"),
                    years=s.get("years"),
                    level=s.get("level"),
                )
            )
        profile.skills = sorted(out, key=lambda sk: sk.name.lower())
        self.store.touch("profiles")
