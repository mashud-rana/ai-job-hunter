"""Shared HTTP client: timeouts, retries with backoff, per-host rate limiting."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.logging import get_logger

log = get_logger(__name__)

USER_AGENT = "AIJobHunter/0.1 (+personal job alert aggregator; respects robots and API terms)"
RETRY_STATUS = {429, 500, 502, 503, 504}


class HostRateLimiter:
    """Minimum interval between requests to the same host."""

    def __init__(self, rate_per_second: float) -> None:
        self.interval = 1.0 / rate_per_second if rate_per_second > 0 else 0.0
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last: dict[str, float] = {}

    async def wait(self, host: str) -> None:
        if not self.interval:
            return
        async with self._locks[host]:
            now = time.monotonic()
            delay = self._last.get(host, 0.0) + self.interval - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._last[host] = time.monotonic()


class RetryableStatusError(httpx.HTTPStatusError):
    pass


def _should_retry(exc: BaseException) -> bool:
    return isinstance(exc, RetryableStatusError | httpx.TransportError)


class HttpClient:
    def __init__(
        self,
        timeout: float = 20.0,
        max_retries: int = 3,
        rate_per_host: float = 1.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"},
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self.max_retries = max(1, max_retries)
        self.limiter = HostRateLimiter(rate_per_host)

    @property
    def raw(self) -> httpx.AsyncClient:
        return self._client

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential_jitter(initial=1, max=30),
            retry=retry_if_exception(_should_retry),
            reraise=True,
        ):
            with attempt:
                await self.limiter.wait(host)
                response = await self._client.request(method, url, **kwargs)
                if response.status_code in RETRY_STATUS:
                    log.warning("http_retryable_status", host=host, status=response.status_code)
                    raise RetryableStatusError(
                        f"{response.status_code} from {host}", request=response.request, response=response
                    )
                response.raise_for_status()
                return response
        raise RuntimeError("unreachable")  # pragma: no cover

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        response = await self.request("GET", url, **kwargs)
        return response.json()

    async def get_text(self, url: str, **kwargs: Any) -> str:
        response = await self.request("GET", url, **kwargs)
        return response.text

    async def aclose(self) -> None:
        await self._client.aclose()
