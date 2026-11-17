"""Shared fixtures. Tests need no API keys, network or database: data goes to JSON files in tmp_path."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import respx

from app.config import Settings
from app.container import Container
from app.database.store import JsonStore


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch):
    """Fail loudly if a test tries to reach the internet. Every hostname connection (sync or asyncio,
    httpx or the OpenAI SDK's httpx2) resolves through socket.getaddrinfo; only loopback is allowed."""
    import socket

    real_getaddrinfo = socket.getaddrinfo
    allowed = {"localhost", "127.0.0.1", "::1"}

    def guarded(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name is not None and name not in allowed:
            raise OSError(f"external network access blocked in tests: {name}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        api_key="test-key",
        storage_backend="local",
        sync_token="sync-secret",
        log_json=False,
        ai_provider="none",
        run_scheduler=False,
        run_bot=False,
        storage_dir=tmp_path / "storage",
        http_max_retries=2,
        http_rate_per_host=0,
        telegram_bot_token="",
        openai_api_key="",
        adzuna_app_id="",
        adzuna_app_key="",
        imap_host="",
        rate_limit="1000/minute",
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[JsonStore]:
    store = JsonStore(settings)
    await store.load()
    yield store
    await store.close()


@pytest.fixture
async def session(db: JsonStore):
    async with db.session() as s:
        yield s


@pytest.fixture
def sender():
    from tests.fakes import FakeSender

    return FakeSender()


@pytest.fixture
async def container(settings: Settings, db: JsonStore, sender) -> AsyncIterator[Container]:
    settings.telegram_allowed_chat_ids = [111]
    c = Container.build(settings, db=db, providers=[], collectors=[], sender=sender)
    yield c
    await c.http.aclose()


@pytest.fixture
def mock_http():
    with respx.mock(assert_all_called=False) as router:
        yield router
