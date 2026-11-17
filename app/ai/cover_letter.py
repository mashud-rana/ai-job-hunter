"""Cover letter / application email / LinkedIn message generation. Requires an AI provider."""

from __future__ import annotations

from app.ai.job_matcher import JobData, JobMatcher, ProfileData
from app.ai.prompts import DOC_INSTRUCTIONS, DOC_SYSTEM, fence_untrusted
from app.ai.providers import LLMError, LLMProvider, LLMResult
from app.logging import get_logger

log = get_logger(__name__)

DOCUMENT_KINDS = tuple(DOC_INSTRUCTIONS)
MAX_TOKENS = {"cover_letter": 900, "email": 450, "linkedin_message": 200}


class GenerationUnavailable(RuntimeError):
    pass


async def generate_document(kind: str, job: JobData, profile: ProfileData, providers: list[LLMProvider]) -> LLMResult:
    if kind not in DOC_INSTRUCTIONS:
        raise ValueError(f"unknown document kind {kind!r}")
    if not providers:
        raise GenerationUnavailable("No AI provider configured (set AI_PROVIDER and its credentials)")
    job_block, _ = fence_untrusted(JobMatcher._job_block(job), 6000)
    profile_block = profile.prompt_block()
    if profile.summary:
        profile_block += f"\nSummary: {profile.summary}"
    prompt = (
        f"<candidate_profile>\n{profile_block}\n</candidate_profile>\n\n"
        f"<job_posting>\n{job_block}\n</job_posting>\n\n{DOC_INSTRUCTIONS[kind]}"
    )
    last_error: Exception | None = None
    for provider in providers:
        try:
            result = await provider.complete(DOC_SYSTEM, prompt, max_tokens=MAX_TOKENS[kind])
        except LLMError as exc:
            last_error = exc
            log.warning("document_generation_failed", provider=provider.name, error=str(exc)[:300])
            continue
        text = result.content.strip()
        if kind == "linkedin_message" and len(text) > 300:
            text = text[:297].rsplit(" ", 1)[0] + "…"
        result.content = text
        return result
    raise GenerationUnavailable(f"All AI providers failed: {last_error}")
