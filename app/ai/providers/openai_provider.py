from __future__ import annotations

from typing import Any

import openai

from app.ai.providers.base import LLMError, LLMProvider, LLMResult, LLMTransportError


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        base_url: str | None = None,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        if not api_key and client is None:
            raise ValueError("OPENAI_API_KEY is required for the OpenAI provider")
        self.model = model
        self._client = client or openai.AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=2)

    async def complete(
        self, system: str, user: str, *, json_schema: dict[str, Any] | None = None, max_tokens: int = 800
    ) -> LLMResult:
        kwargs: dict[str, Any] = {}
        if json_schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_completion_tokens=max_tokens,
                **kwargs,
            )
        except openai.APIStatusError as exc:
            # Don't propagate the server message: e.g. 401 bodies echo (part of) the API key.
            raise LLMTransportError(f"OpenAI request failed: HTTP {exc.status_code} {type(exc).__name__}") from None
        except openai.OpenAIError as exc:
            raise LLMTransportError(f"OpenAI request failed: {type(exc).__name__}") from None
        if not resp.choices or not resp.choices[0].message.content:
            raise LLMError("OpenAI returned an empty response")
        return LLMResult(content=resp.choices[0].message.content, provider=self.name, model=self.model)

    async def aclose(self) -> None:
        await self._client.close()
