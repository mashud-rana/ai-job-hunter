from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import APIKeyHeader

from app.container import Container
from app.database.repositories import Page

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_container(request: Request) -> Container:
    return request.app.state.container


ContainerDep = Annotated[Container, Depends(get_container)]


def require_api_key(request: Request, key: Annotated[str | None, Depends(api_key_header)]) -> None:
    expected = get_container(request).settings.api_key
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API_KEY is not configured on the server")
    if not key or not secrets.compare_digest(key.encode(), expected.encode()):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or missing X-API-Key", headers={"WWW-Authenticate": "APIKey"}
        )


class Pagination:
    def __init__(
        self,
        page: Annotated[int, Query(ge=1, le=10_000)] = 1,
        per_page: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> None:
        self.page = page
        self.per_page = per_page


PaginationDep = Annotated[Pagination, Depends()]


def page_meta(page: Page) -> dict[str, int]:
    return {"total": page.total, "page": page.page, "per_page": page.per_page, "pages": page.pages}


# ---- dashboard session (single-user; cookie = HMAC of the API key, never the key itself)
DASHBOARD_COOKIE = "jh_session"


def session_token(api_key: str) -> str:
    return hmac.new(api_key.encode(), b"dashboard-session-v1", hashlib.sha256).hexdigest()


def csrf_token(api_key: str) -> str:
    return hmac.new(api_key.encode(), b"dashboard-csrf-v1", hashlib.sha256).hexdigest()[:32]


def dashboard_authenticated(request: Request) -> bool:
    key = get_container(request).settings.api_key
    cookie = request.cookies.get(DASHBOARD_COOKIE, "")
    return bool(key) and secrets.compare_digest(cookie.encode(), session_token(key).encode())
