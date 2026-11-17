from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class LLMError(RuntimeError):
    """Provider call failed (network, auth, rate limit, malformed output)."""


class LLMTransportError(LLMError):
    """The request itself failed (network, auth, quota). Retrying the same provider won't help."""


@dataclass
class LLMResult:
    content: str
    provider: str
    model: str

    def json(self) -> dict[str, Any]:
        return parse_json_object(self.content)


class LLMProvider(ABC):
    name: str
    model: str

    @abstractmethod
    async def complete(
        self,
        system: str,
        user: str,
        *,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 800,
    ) -> LLMResult:
        """Return the model's reply. When json_schema is given, ask for a single JSON object."""

    async def aclose(self) -> None:
        return None


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from model output, tolerating code fences / leading prose."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("model did not return JSON") from None
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"invalid JSON from model: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMError("model returned JSON that is not an object")
    return value
