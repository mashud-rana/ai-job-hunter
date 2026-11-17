from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Recommendation = Literal["apply", "consider", "skip"]


def score_category(score: int) -> str:
    if score >= 90:
        return "excellent"
    if score >= 80:
        return "strong"
    if score >= 70:
        return "good"
    if score >= 50:
        return "moderate"
    return "poor"


CATEGORY_LABELS = {
    "excellent": "Excellent Match",
    "strong": "Strong Match",
    "good": "Good Match",
    "moderate": "Moderate Match",
    "poor": "Poor Match",
}


class MatchResult(BaseModel):
    """Validated AI (or rules) match output."""

    match_score: int = Field(ge=0, le=100)
    matched_skills: list[str] = Field(default_factory=list, max_length=40)
    missing_skills: list[str] = Field(default_factory=list, max_length=40)
    experience_match: bool | None = None
    location_eligible: bool | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    ai_reason: str = Field(default="", max_length=1000)
    recommendation: Recommendation

    @field_validator("matched_skills", "missing_skills", mode="before")
    @classmethod
    def _clean_list(cls, v: object) -> object:
        if v is None:
            return []
        if isinstance(v, list):
            out: list[str] = []
            for item in v:
                s = str(item).strip()[:60]
                if s and s.lower() not in {x.lower() for x in out}:
                    out.append(s)
            return out[:40]
        return v

    @field_validator("match_score", mode="before")
    @classmethod
    def _round_score(cls, v: object) -> object:
        if isinstance(v, float):
            return round(v)
        return v

    @field_validator("ai_reason", mode="before")
    @classmethod
    def _trim_reason(cls, v: object) -> object:
        return str(v or "")[:1000]

    @property
    def category(self) -> str:
        return score_category(self.match_score)


MATCH_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "match_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
        "experience_match": {"type": ["boolean", "null"]},
        "location_eligible": {"type": ["boolean", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "ai_reason": {"type": "string"},
        "recommendation": {"type": "string", "enum": ["apply", "consider", "skip"]},
    },
    "required": [
        "match_score",
        "matched_skills",
        "missing_skills",
        "confidence",
        "ai_reason",
        "recommendation",
    ],
}
