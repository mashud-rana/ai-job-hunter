from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    category: str | None = Field(default=None, max_length=50)
    years: float | None = Field(default=None, ge=0, le=60)
    level: str | None = Field(default=None, max_length=30)


class SkillOut(SkillIn):
    model_config = ConfigDict(from_attributes=True)


class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    headline: str | None = Field(default=None, max_length=255)
    years_experience: float = Field(default=0, ge=0, le=60)
    target_titles: list[str] = Field(default_factory=list, max_length=50)
    preferred_locations: list[str] = Field(default_factory=list, max_length=50)
    job_types: list[str] = Field(default_factory=list, max_length=20)
    min_salary: float | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, max_length=8)
    summary: str | None = Field(default=None, max_length=5000)
    skills: list[SkillIn] = Field(default_factory=list, max_length=200)

    @field_validator("target_titles", "preferred_locations", "job_types")
    @classmethod
    def _strip(cls, v: list[str]) -> list[str]:
        return [s.strip()[:120] for s in v if s and s.strip()]


class ProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    headline: str | None
    years_experience: float
    target_titles: list[str]
    preferred_locations: list[str]
    job_types: list[str]
    min_salary: float | None
    salary_currency: str | None
    summary: str | None
    is_verified: bool
    version: int
    cv_filename: str | None
    skills: list[SkillOut]
    pending_profile: dict[str, Any] | None
    updated_at: datetime


class ExtractedSkill(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    category: str | None = Field(default=None, max_length=50)


class ProfileExtraction(BaseModel):
    """What the CV parser produced; stored as pending until the user confirms it."""

    name: str | None = Field(default=None, max_length=255)
    headline: str | None = Field(default=None, max_length=255)
    years_experience: float | None = Field(default=None, ge=0, le=60)
    target_titles: list[str] = Field(default_factory=list, max_length=30)
    skills: list[ExtractedSkill] = Field(default_factory=list, max_length=150)
    preferred_locations: list[str] = Field(default_factory=list, max_length=20)
    summary: str | None = Field(default=None, max_length=2000)
    method: str = "rules"  # rules | openai | ollama

    @field_validator("target_titles", "preferred_locations", mode="before")
    @classmethod
    def _clean(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x).strip()[:120] for x in v if str(x).strip()]
        return v

    @field_validator("skills", mode="before")
    @classmethod
    def _skills(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, list):
            return [{"name": x} if isinstance(x, str) else x for x in v]
        return v


CV_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "headline": {"type": ["string", "null"]},
        "years_experience": {"type": ["number", "null"]},
        "target_titles": {"type": "array", "items": {"type": "string"}},
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "category": {"type": ["string", "null"]}},
                "required": ["name"],
            },
        },
        "preferred_locations": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": ["string", "null"]},
    },
    "required": ["skills", "target_titles"],
}
