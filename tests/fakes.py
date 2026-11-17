"""Test doubles."""

from __future__ import annotations

import json
from typing import Any

from app.ai.job_matcher import JobData, ProfileData
from app.ai.providers import LLMError, LLMProvider, LLMResult, LLMTransportError


class FakeProvider(LLMProvider):
    """Returns queued responses; an Exception instance in the queue is raised."""

    def __init__(self, responses: list[Any], name: str = "fake", model: str = "fake-1") -> None:
        self.responses = list(responses)
        self.name = name
        self.model = model
        self.calls: list[dict[str, Any]] = []

    async def complete(self, system: str, user: str, *, json_schema=None, max_tokens: int = 800) -> LLMResult:
        self.calls.append({"system": system, "user": user, "json_schema": json_schema})
        if not self.responses:
            raise LLMTransportError("no more fake responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        content = item if isinstance(item, str) else json.dumps(item)
        return LLMResult(content=content, provider=self.name, model=self.model)


__all__ = ["FakeProvider", "LLMError", "LLMTransportError", "make_job", "make_profile"]


def make_profile(**kw: Any) -> ProfileData:
    data: dict[str, Any] = dict(
        name="Md. Test",
        years_experience=5,
        skills=[
            "PHP",
            "Laravel",
            "Python",
            "FastAPI",
            "MySQL",
            "PostgreSQL",
            "Redis",
            "Docker",
            "AWS",
            "Vue.js",
            "React",
            "REST API",
        ],
        target_titles=["Senior Laravel Developer", "Senior PHP Developer", "Python Backend Developer"],
        preferred_locations=["Bangladesh", "Worldwide Remote"],
        job_types=["Full-time", "Contract"],
    )
    data.update(kw)
    return ProfileData(**data)


def make_job(**kw: Any) -> JobData:
    data: dict[str, Any] = dict(
        title="Senior Laravel Developer",
        company="Acme",
        location="Worldwide",
        country="Worldwide",
        remote_type="remote",
        job_type="Full-time",
        description="We need 4+ years of experience with PHP, Laravel, MySQL, Redis and Docker. Kubernetes is a plus.",
        salary_text=None,
        salary_min=None,
        salary_max=None,
        currency=None,
        tags=[],
    )
    data.update(kw)
    return JobData(**data)


class FakeSender:
    """Records outbound Telegram messages; set fail=True to simulate delivery errors."""

    def __init__(self, fail: bool = False) -> None:
        self.messages: list[dict[str, Any]] = []
        self.fail = fail

    async def send(self, chat_id: int, text: str, reply_markup: Any = None) -> None:
        if self.fail:
            from telegram.error import NetworkError

            raise NetworkError("simulated outage")
        self.messages.append({"chat_id": chat_id, "text": text, "markup": reply_markup})
