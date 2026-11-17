"""Minimal in-process rate limiter (fixed window per client IP).

Single-process deployments (this app runs one worker because the scheduler and bot live in-process)
don't need a shared store. Behind a reverse proxy run uvicorn with --proxy-headers so request.client
is the real client address.
"""

from __future__ import annotations

import math
import re
import time

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_UNITS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}
_SPEC = re.compile(r"^\s*(\d+)\s*/\s*(second|minute|hour|day)s?\s*$", re.I)
EXEMPT_PATHS = ("/api/health",)  # monitoring probes shouldn't be throttled


def parse_limit(spec: str) -> tuple[int, int] | None:
    """'60/minute' -> (60, 60). 'disabled' or '' -> None."""
    if not spec or spec.strip().lower() in {"disabled", "off", "none"}:
        return None
    m = _SPEC.match(spec)
    if not m:
        raise ValueError(f"invalid RATE_LIMIT {spec!r}; use e.g. '60/minute'")
    return int(m.group(1)), _UNITS[m.group(2).lower()]


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, limit: str, exempt: tuple[str, ...] = EXEMPT_PATHS) -> None:
        self.app = app
        self.rule = parse_limit(limit)
        self.exempt = exempt
        self._hits: dict[str, tuple[int, int]] = {}  # ip -> (window index, count)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.rule is None or scope.get("path", "").startswith(self.exempt):
            await self.app(scope, receive, send)
            return
        max_requests, window = self.rule
        client = scope.get("client")
        ip = client[0] if client else "unknown"
        now = time.time()
        current = int(now // window)
        win, count = self._hits.get(ip, (current, 0))
        if win != current:
            win, count = current, 0
        count += 1
        self._hits[ip] = (win, count)
        if len(self._hits) > 10_000:  # bound memory: drop stale windows
            self._hits = {k: v for k, v in self._hits.items() if v[0] == current}
        if count > max_requests:
            retry = max(1, math.ceil((win + 1) * window - now))
            response = JSONResponse(
                {"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": str(retry)}
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
