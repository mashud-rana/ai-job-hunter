from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class Page(Generic[T]):
    items: list[T]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page)) if self.per_page else 1


def paginate(items: list[T], page: int, per_page: int) -> Page[T]:
    page = max(1, page)
    per_page = max(1, min(per_page, 100))
    start = (page - 1) * per_page
    return Page(items=items[start : start + per_page], total=len(items), page=page, per_page=per_page)
