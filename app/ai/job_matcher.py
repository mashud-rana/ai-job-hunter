"""Two-stage job matching: deterministic pre-score, then LLM evaluation for promising jobs.

Stage 1 (rules) is free and always runs. Jobs below PREFILTER_MIN_SCORE are stored with the
rules result and never sent to the LLM. Stage 2 output is schema-validated and cross-checked
against the actual job text so the model cannot invent skills.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from app.ai.prompts import MATCH_SYSTEM, fence_untrusted, match_user_prompt
from app.ai.providers import LLMError, LLMProvider, LLMTransportError
from app.ai.skills import canonical_skill, detect_skills, mentions
from app.logging import get_logger
from app.schemas.match import MATCH_JSON_SCHEMA, MatchResult

log = get_logger(__name__)

MAX_JOB_CHARS = 7000


@dataclass
class ProfileData:
    name: str
    years_experience: float
    skills: list[str]
    target_titles: list[str] = field(default_factory=list)
    preferred_locations: list[str] = field(default_factory=list)
    job_types: list[str] = field(default_factory=list)
    min_salary: float | None = None
    salary_currency: str | None = None
    headline: str | None = None
    summary: str | None = None

    @property
    def home_country(self) -> str | None:
        for loc in self.preferred_locations:
            if "bangladesh" in loc.lower() or "dhaka" in loc.lower():
                return "Bangladesh"
        return None

    def prompt_block(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "headline": self.headline,
                "years_experience": self.years_experience,
                "target_titles": self.target_titles,
                "skills": self.skills,
                "preferred_locations": self.preferred_locations,
                "employment_types": self.job_types,
                "min_salary": self.min_salary,
                "salary_currency": self.salary_currency,
            },
            ensure_ascii=False,
        )


@dataclass
class JobData:
    title: str
    company: str | None
    location: str | None
    country: str | None
    remote_type: str
    job_type: str | None
    description: str
    salary_text: str | None
    salary_min: float | None
    salary_max: float | None
    currency: str | None
    tags: list[str] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        return f"{self.title}\n{' '.join(self.tags)}\n{self.description}"


@dataclass
class RuleAnalysis:
    score: int
    job_skills: list[str]
    matched: list[str]
    missing: list[str]
    required_years: float | None
    experience_match: bool | None
    location_eligible: bool | None
    seniority: str
    reasons: list[str]

    def hints(self) -> str:
        return json.dumps(
            {
                "skills_detected_in_job": self.job_skills,
                "candidate_skills_found_in_job": self.matched,
                "required_years_detected": self.required_years,
                "seniority_detected": self.seniority,
                "location_eligible_by_rules": self.location_eligible,
            }
        )


@dataclass
class MatchOutcome:
    result: MatchResult
    provider: str
    model: str | None
    rules: RuleAnalysis
    injection_flagged: bool = False


# ---------------------------------------------------------------- rules

_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:-|–|to)?\s*(?:\d{1,2}\s*)?\+?\s*(?:years|yrs)", re.I)
_JUNIOR = re.compile(r"\b(junior|jr\.?|intern(ship)?|trainee|graduate|entry[- ]level|apprentice|werkstudent)\b", re.I)
_SENIOR = re.compile(r"\b(senior|sr\.?|lead|principal|staff|architect|tech lead|team lead)\b", re.I)
_EXEC = re.compile(r"\b(director|vp|vice president|head of|chief|cto)\b", re.I)
_DEV_ROLE = re.compile(
    r"\b(engineer|developer|programmer|software|backend|back-end|full[- ]?stack|devops|sre|architect|tech lead)\b",
    re.I,
)
_CORE_TECH = re.compile(r"\b(php|laravel|python|fastapi|flask|django|backend|back-end|full[- ]?stack|api)\b", re.I)
_RESTRICTED = re.compile(
    r"\b(?:(?:us|usa|u\.s\.|uk|eu|europe|canada|germany)[- ]only|only (?:us|usa|uk|eu)\b|must (?:be )?(?:located|based|"
    r"reside|live)s? in|(?:us|uk|eu) (?:citizens?|residents?) only|work authori[sz]ation (?:in|for) the (?:us|uk))",
    re.I,
)
_GLOBAL = re.compile(
    r"\b(worldwide|anywhere|global(?:ly)?|international|any (?:location|country)|asia|apac|"
    r"south asia|bangladesh|utc\s*\+\s*[5-7])\b",
    re.I,
)
_VISA = re.compile(r"\b(visa sponsorship|sponsor (?:a |your )?visa|relocation (?:support|package|assistance))\b", re.I)
_REGION_LIMITED = re.compile(
    r"\b(latam|latin america|brazil|mexico|argentina|colombia|europe|european union|eu|emea|uk|united kingdom|"
    r"usa|u.s.a?|united states|north america|canada|germany|dach|australia|nz|africa|cet|est|pst)\b",
    re.I,
)
_REMOTE_MENTION = re.compile(r"\b(remote|work from home|home office|homeoffice|wfh)\b", re.I)
_BAD_TYPES = re.compile(r"\b(part[- ]time|internship|werkstudent|working student|minijob)\b", re.I)


def _required_years(text: str) -> float | None:
    values = [int(m.group(1)) for m in _YEARS.finditer(text) if 0 < int(m.group(1)) <= 20]
    return float(min(values)) if values else None


def location_eligibility(job: JobData, profile: ProfileData) -> bool | None:
    home = profile.home_country
    loc = job.location or ""
    text = f"{loc}\n{job.description[:4000]}"
    region_text = f"{job.title} | {loc}"
    if job.remote_type == "remote":
        if _GLOBAL.search(loc) or (home and home.lower() in loc.lower()):
            return True
        if _RESTRICTED.search(text) or _REGION_LIMITED.search(region_text):
            return False  # remote, but only for listed countries/regions/timezones
        if not loc or loc.strip().lower() in {"remote", "remote job", "fully remote", "100% remote"}:
            return True
        return None
    if home and job.country == home:
        return True
    if job.country == "Worldwide":
        return None
    if loc:
        # On-site/hybrid or unspecified work mode in another country.
        if _VISA.search(text):
            return None
        if job.remote_type == "unknown" and _REMOTE_MENTION.search(job.description[:4000]):
            return None  # may allow remote work; let the AI judge
        if home and home.lower() not in loc.lower():
            return False
    return None


def _abroad(job: JobData, profile: ProfileData) -> bool:
    """A concrete location outside the candidate's home country (eligibility not confirmed)."""
    home = profile.home_country
    if job.country in ("Worldwide",) or (home and job.country == home):
        return False
    if job.country:
        return True
    loc = (job.location or "").strip()
    if not loc or _GLOBAL.search(loc) or loc.lower() in {"remote", "remote job", "fully remote", "100% remote"}:
        return False
    return not (home and home.lower() in loc.lower())


def analyze(job: JobData, profile: ProfileData) -> RuleAnalysis:
    text = job.full_text
    cand = {canonical_skill(s).lower(): canonical_skill(s) for s in profile.skills}
    job_skills = detect_skills(text)
    matched = [s for s in job_skills if s.lower() in cand]
    missing = [s for s in job_skills if s.lower() not in cand]
    reasons: list[str] = []

    # skills (40)
    if job_skills:
        ratio = len(matched) / len(job_skills)
        breadth = min(1.0, len(matched) / 4)  # one shared keyword isn't a strong stack match
        skills_pts = 40 * min(1.0, ratio * 1.25) * (0.4 + 0.6 * breadth)
        if any(s in matched for s in ("PHP", "Laravel", "Python")):
            skills_pts = min(40, skills_pts + 5)
    else:
        skills_pts = 14.0
        reasons.append("no recognisable tech stack in posting")

    # title (20)
    title = job.title.lower()
    if any(t.lower() in title for t in profile.target_titles):
        title_pts = 20
    elif _CORE_TECH.search(job.title) and _DEV_ROLE.search(job.title):
        title_pts = 16
    elif _CORE_TECH.search(job.title):
        title_pts = 8  # stack word in a non-engineering title, e.g. "Operations Expert with Python"
    elif _DEV_ROLE.search(job.title):
        title_pts = 10
    else:
        title_pts = 0
        reasons.append("title is not a software engineering role")

    # seniority (10)
    if _JUNIOR.search(job.title):
        seniority, sen_pts = "junior", 2
    elif _EXEC.search(job.title):
        seniority, sen_pts = "executive", 4
    elif _SENIOR.search(job.title):
        seniority, sen_pts = "senior", 10
    else:
        seniority, sen_pts = "mid", 7

    # experience (10)
    req = _required_years(job.description)
    if req is None:
        exp_pts, exp_match = 7, None
    elif req <= profile.years_experience:
        exp_pts, exp_match = 10, True
    elif req <= profile.years_experience + 2:
        exp_pts, exp_match = 5, False
    else:
        exp_pts, exp_match = 0, False
        reasons.append(f"requires {req:g}+ years")

    # location (15)
    loc_ok = location_eligibility(job, profile)
    loc_pts = {True: 15, None: 8, False: 0}[loc_ok]
    if loc_ok is False:
        reasons.append("location not eligible")

    # type & salary (5)
    jt = f"{job.job_type or ''} {job.title}"
    type_pts = 1 if _BAD_TYPES.search(jt) else (5 if job.job_type else 3)
    if (
        profile.min_salary
        and job.salary_max
        and job.currency
        and profile.salary_currency
        and job.currency.upper() == profile.salary_currency.upper()
        and job.salary_max < profile.min_salary
    ):
        type_pts = 0
        reasons.append("salary below minimum")

    score = round(skills_pts + title_pts + sen_pts + exp_pts + loc_pts + type_pts)
    if loc_ok is False:
        score = min(score, 45)
    elif loc_ok is None and _abroad(job, profile):
        # Abroad with unconfirmed eligibility: never "strong" on rules alone (no alert spam).
        score = min(score, 75)
    if title_pts == 0:
        # Non-engineering title: only worth an AI look if the posting uses the candidate's stack.
        score = min(score, 45 if matched else 25)
    return RuleAnalysis(
        score=max(0, min(100, score)),
        job_skills=job_skills,
        matched=matched,
        missing=missing,
        required_years=req,
        experience_match=exp_match,
        location_eligible=loc_ok,
        seniority=seniority,
        reasons=reasons,
    )


def rules_result(analysis: RuleAnalysis) -> MatchResult:
    s = analysis.score
    reason = "Rule-based estimate"
    if analysis.matched:
        reason += f"; matches {', '.join(analysis.matched[:6])}"
    if analysis.reasons:
        reason += f"; {'; '.join(analysis.reasons)}"
    return MatchResult(
        match_score=s,
        matched_skills=analysis.matched,
        missing_skills=analysis.missing,
        experience_match=analysis.experience_match,
        location_eligible=analysis.location_eligible,
        confidence=0.4,
        ai_reason=reason + ".",
        recommendation="apply" if s >= 75 else "consider" if s >= 55 else "skip",
    )


# ---------------------------------------------------------------- matcher


class JobMatcher:
    def __init__(self, providers: list[LLMProvider], prefilter_min_score: int = 35) -> None:
        self.providers = providers
        self.prefilter_min_score = prefilter_min_score

    async def match(self, job: JobData, profile: ProfileData) -> MatchOutcome:
        analysis = analyze(job, profile)
        if not self.providers or analysis.score < self.prefilter_min_score:
            return MatchOutcome(rules_result(analysis), "rules", None, analysis)

        job_block, flagged = fence_untrusted(self._job_block(job), MAX_JOB_CHARS)
        if flagged:
            log.warning("prompt_injection_suspected", title=job.title[:80], company=job.company)
        prompt = match_user_prompt(profile.prompt_block(), job_block, analysis.hints())

        for provider in self.providers:
            for attempt in (1, 2):
                try:
                    res = await provider.complete(MATCH_SYSTEM, prompt, json_schema=MATCH_JSON_SCHEMA, max_tokens=600)
                    parsed = MatchResult.model_validate(res.json())
                except LLMTransportError as exc:
                    log.warning("ai_provider_unreachable", provider=provider.name, error=str(exc)[:300])
                    break  # go straight to the fallback provider
                except (LLMError, ValidationError) as exc:
                    # malformed / invalid output: ask the same provider once more
                    log.warning("ai_match_invalid", provider=provider.name, attempt=attempt, error=str(exc)[:300])
                    continue
                result = self._verify(parsed, job, profile, analysis, flagged)
                return MatchOutcome(result, res.provider, res.model, analysis, flagged)
        log.warning("ai_match_all_providers_failed", title=job.title[:80])
        return MatchOutcome(rules_result(analysis), "rules", None, analysis, flagged)

    @staticmethod
    def _job_block(job: JobData) -> str:
        return (
            f"Title: {job.title}\nCompany: {job.company or 'unknown'}\nLocation: {job.location or 'unknown'}\n"
            f"Work mode: {job.remote_type}\nEmployment type: {job.job_type or 'unknown'}\n"
            f"Salary: {job.salary_text or 'not specified'}\nTags: {', '.join(job.tags) or 'none'}\n\n"
            f"Description:\n{job.description or '(no description provided)'}"
        )

    @staticmethod
    def _verify(
        r: MatchResult, job: JobData, profile: ProfileData, analysis: RuleAnalysis, flagged: bool
    ) -> MatchResult:
        """Ground the model output in the actual texts (no invented skills / facts)."""
        text = job.full_text
        cand = {canonical_skill(s).lower() for s in profile.skills}
        matched = [s for s in r.matched_skills if canonical_skill(s).lower() in cand and mentions(text, s)]
        for s in analysis.matched:  # keep verified keyword matches the model forgot
            if s not in matched:
                matched.append(s)
        missing = [s for s in r.missing_skills if canonical_skill(s).lower() not in cand and mentions(text, s)]
        score = r.match_score
        confidence = r.confidence
        if flagged:
            # The posting tried to steer the model: never let it lift the score far above the rules.
            score = min(score, analysis.score + 10)
            confidence = min(confidence, 0.5)
        if analysis.location_eligible is False and r.location_eligible is not True:
            score = min(score, 49)
        rec = r.recommendation
        if score < 50 and rec == "apply":
            rec = "consider"
        return r.model_copy(
            update={
                "match_score": score,
                "matched_skills": matched,
                "missing_skills": missing,
                "confidence": confidence,
                "recommendation": rec,
                # trust only booleans the model actually set; fall back to verified rule facts
                "experience_match": r.experience_match if r.experience_match is not None else analysis.experience_match,
                "location_eligible": r.location_eligible
                if r.location_eligible is not None
                else analysis.location_eligible,
            }
        )
