"""FastAPI application factory and process lifespan (DB, scheduler, Telegram bot)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response

from app.api import applications, cron, health, html, jobs, matches, profile, sources
from app.api.ratelimit import RateLimitMiddleware
from app.config import Settings, get_settings
from app.container import Container
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


async def start_runtime(container: Container) -> None:
    """Start background components according to RUN_SCHEDULER / RUN_BOT."""
    s = container.settings
    if not s.api_key:
        log.error("api_key_missing", impact="API and dashboard will refuse all requests until API_KEY is set")
    await container.startup()
    await container.services.collection.sync_sources()
    if s.run_scheduler:
        from app.scheduler import tasks
        from app.scheduler.setup import create_scheduler, register_jobs

        tasks.register_runtime(container)
        scheduler = create_scheduler(s)
        scheduler.start(paused=True)  # load persisted jobs before reconciling
        register_jobs(scheduler, s)
        scheduler.resume()
        container.extras["scheduler"] = scheduler
        log.info("scheduler_started", jobs=[f"{j.id}@{j.next_run_time}" for j in scheduler.get_jobs()])
    if s.run_bot:
        from app.bots.telegram_bot import build_bot

        bot = build_bot(container)
        if bot is not None:
            try:
                await bot.start()
                container.extras["bot"] = bot
            except Exception:
                # A bad token / network issue must not take down the API and scheduler.
                log.exception("telegram_bot_start_failed")


async def stop_runtime(container: Container) -> None:
    scheduler = container.extras.pop("scheduler", None)
    if scheduler is not None:
        from app.scheduler import tasks

        scheduler.shutdown(wait=False)  # stop scheduling new runs
        await tasks.wait_for_running(grace_seconds=30)  # let in-flight runs finish
        tasks.register_runtime(None)
    bot = container.extras.pop("bot", None)
    if bot is not None:
        await bot.stop()


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        c = container or Container.build(settings)
        app.state.container = c
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        await start_runtime(c)
        log.info("app_started", env=settings.app_env)
        try:
            yield
        finally:
            log.info("app_stopping")
            await stop_runtime(c)
            await c.aclose()
            log.info("app_stopped")

    is_prod = settings.app_env == "production"
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )

    # Per-client-IP rate limiting for every route except /api/health. RATE_LIMIT=disabled turns it off.
    app.add_middleware(RateLimitMiddleware, limit=settings.rate_limit)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith(("/docs", "/openapi")):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
        if is_prod:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response

    @app.exception_handler(html.LoginRequired)
    async def _login_redirect(request: Request, exc: html.LoginRequired) -> RedirectResponse:
        return RedirectResponse("/login", status_code=303)

    for router in (
        health.router,
        cron.router,
        jobs.router,
        matches.router,
        applications.router,
        profile.router,
        sources.router,
        html.router,
    ):
        app.include_router(router)
    return app


app = create_app()
