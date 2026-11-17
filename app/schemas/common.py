from __future__ import annotations

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Paginated(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    per_page: int
    pages: int


class SourceOut(BaseModel):
    key: str
    name: str
    enabled: bool
    available: bool
    unavailable_reason: str | None
    last_run_at: str | None
    last_success_at: str | None


class DocumentOut(BaseModel):
    id: int
    job_id: int | None
    kind: str
    content: str
    provider: str | None
    model: str | None
    created_at: str
