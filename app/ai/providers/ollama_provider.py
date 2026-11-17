"""Ollama local model provider (https://github.com/ollama/ollama/blob/main/docs/api.md)."""

from __future__ import annotations

from typing import Any

import httpx

from app.ai.providers.base import LLMError, LLMProvider, LLMResult, LLMTransportError


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self, base_url: str, model: str, timeout: float = 120.0, client: httpx.AsyncClient | None = None
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    async def complete(
        self, system: str, user: str, *, json_schema: dict[str, Any] | None = None, max_tokens: int = 800
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": max_tokens},
        }
        if json_schema is not None:
            # Ollama >= 0.5 supports structured outputs with a JSON schema in "format".
            payload["format"] = json_schema
        try:
            resp = await self._client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise LLMTransportError(f"Ollama request failed: {type(exc).__name__}: {exc}") from exc
        content = (data.get("message") or {}).get("content")
        if not content:
            raise LLMError("Ollama returned an empty response")
        return LLMResult(content=content, provider=self.name, model=self.model)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
