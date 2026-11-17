"""PDF CV text extraction and candidate profile extraction (rules + optional AI)."""

from __future__ import annotations

import re

import pymupdf
from pydantic import ValidationError

from app.ai.prompts import CV_SYSTEM, fence_untrusted
from app.ai.providers import LLMError, LLMProvider
from app.ai.skills import canonical_skill, detect_skills, mentions, skill_category
from app.logging import get_logger
from app.schemas.profile import CV_JSON_SCHEMA, ExtractedSkill, ProfileExtraction
from app.utils.text import clean_text

log = get_logger(__name__)

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PAGES = 15
MAX_TEXT = 40_000


class CVError(ValueError):
    pass


def validate_pdf(data: bytes) -> None:
    if not data:
        raise CVError("empty file")
    if len(data) > MAX_PDF_BYTES:
        raise CVError(f"file too large (max {MAX_PDF_BYTES // (1024 * 1024)} MB)")
    if not data[:1024].lstrip().startswith(b"%PDF-"):
        raise CVError("file is not a PDF")


def extract_pdf_text(data: bytes) -> str:
    validate_pdf(data)
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise CVError("could not open PDF") from exc
    try:
        if doc.needs_pass:
            raise CVError("password-protected PDFs are not supported")
        if doc.page_count > MAX_PAGES:
            raise CVError(f"PDF has too many pages (max {MAX_PAGES})")
        text = "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()
    text = clean_text(text)[:MAX_TEXT]
    if len(text) < 50:
        raise CVError("no extractable text (scanned PDF?); OCR is not supported")
    return text


_YEARS = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:years|yrs)(?:\s+of)?\s+(?:professional\s+|industry\s+|hands-on\s+)?"
    r"(?:experience|exp)",
    re.I,
)
_TITLE = re.compile(
    r"\b((?:senior|sr\.?|lead|principal|staff|junior|associate)?\s*(?:full[- ]?stack|back[- ]?end|front[- ]?end|php|"
    r"laravel|python|software|web|devops)\s+(?:developer|engineer|architect)|(?:php|engineering|tech(?:nical)?)\s+"
    r"team\s+lead|team\s+lead|software\s+architect|tech\s+lead)\b",
    re.I,
)
_LOCATIONS = ("Dhaka", "Bangladesh", "Chittagong", "Sylhet", "Remote")


def rules_extract(text: str) -> ProfileExtraction:
    lines = [ln for ln in text.split("\n") if ln.strip()]
    name = None
    for ln in lines[:5]:
        candidate = ln.strip()
        if 2 <= len(candidate.split()) <= 6 and not re.search(r"[@\d:/|]", candidate) and len(candidate) < 60:
            name = candidate
            break
    years = [int(m.group(1)) for m in _YEARS.finditer(text) if 0 < int(m.group(1)) <= 50]
    titles: list[str] = []
    for m in _TITLE.finditer(text):
        t = " ".join(m.group(1).split()).title()
        if t.lower() not in {x.lower() for x in titles}:
            titles.append(t)
    skills = [{"name": s, "category": skill_category(s)} for s in detect_skills(text)]
    locations = [loc for loc in _LOCATIONS if re.search(rf"\b{loc}\b", text, re.I)]
    return ProfileExtraction(
        name=name,
        headline=titles[0] if titles else None,
        years_experience=float(max(years)) if years else None,
        target_titles=titles[:10],
        skills=skills,  # type: ignore[arg-type]
        preferred_locations=locations,
        summary=None,
        method="rules",
    )


async def parse_cv(text: str, providers: list[LLMProvider]) -> ProfileExtraction:
    rules = rules_extract(text)
    if not providers:
        return rules
    fenced, flagged = fence_untrusted(text, 15_000)
    if flagged:
        log.warning("cv_prompt_injection_suspected")
    prompt = f"<cv_text>\n{fenced}\n</cv_text>\n\nReturn the JSON object now."
    for provider in providers:
        try:
            res = await provider.complete(CV_SYSTEM, prompt, json_schema=CV_JSON_SCHEMA, max_tokens=1500)
            ai = ProfileExtraction.model_validate(res.json())
        except (LLMError, ValidationError) as exc:
            log.warning("cv_ai_extraction_failed", provider=provider.name, error=str(exc)[:300])
            continue
        return _ground(ai, rules, text, res.provider)
    return rules


def _ground(ai: ProfileExtraction, rules: ProfileExtraction, text: str, method: str) -> ProfileExtraction:
    """Keep only AI-claimed skills that literally appear in the CV; union with rule-detected skills."""
    seen: set[str] = set()
    skills = []
    for s in [*ai.skills, *rules.skills]:
        canon = canonical_skill(s.name)
        if canon.lower() in seen or not mentions(text, s.name):
            continue
        seen.add(canon.lower())
        skills.append({"name": canon, "category": s.category or skill_category(canon)})
    years = ai.years_experience
    if years is not None and rules.years_experience is not None and years > rules.years_experience + 3:
        years = rules.years_experience  # model over-estimated relative to what the CV states
    return ai.model_copy(
        update={
            "skills": [ExtractedSkill(**s) for s in skills],
            "years_experience": years if years is not None else rules.years_experience,
            "name": ai.name or rules.name,
            "method": method,
        }
    )
