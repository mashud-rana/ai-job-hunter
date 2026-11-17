"""Provider factory. Select with AI_PROVIDER / AI_FALLBACK_PROVIDER (openai | ollama | none)."""

from __future__ import annotations

from app.ai.providers.base import LLMError, LLMProvider, LLMResult, LLMTransportError, parse_json_object
from app.config import Settings
from app.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMResult",
    "LLMTransportError",
    "build_provider_chain",
    "parse_json_object",
]


def _build_one(kind: str, settings: Settings) -> LLMProvider | None:
    if kind == "openai":
        if not settings.openai_api_key:
            log.warning("ai_provider_unconfigured", provider="openai", reason="OPENAI_API_KEY missing")
            return None
        from app.ai.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(
            settings.openai_api_key, settings.openai_model, settings.ai_timeout, settings.openai_base_url
        )
    if kind == "ollama":
        from app.ai.providers.ollama_provider import OllamaProvider

        return OllamaProvider(settings.ollama_base_url, settings.ollama_model, settings.ai_timeout)
    return None


def build_provider_chain(settings: Settings) -> list[LLMProvider]:
    """Primary first, then fallback. Empty list means rules-only matching."""
    chain: list[LLMProvider] = []
    for kind in (settings.ai_provider, settings.ai_fallback_provider):
        if kind == "none" or any(p.name == kind for p in chain):
            continue
        provider = _build_one(kind, settings)
        if provider:
            chain.append(provider)
    return chain
