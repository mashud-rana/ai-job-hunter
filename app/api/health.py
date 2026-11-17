from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.container import Container

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health(request: Request) -> JSONResponse:
    container: Container = request.app.state.container
    checks: dict[str, Any] = {}
    store = container.db
    checks["storage"] = "ok" if store.loaded and not store.last_error else f"error: {store.last_error or 'not loaded'}"
    checks["storage_backend"] = store.backend.name
    scheduler = container.extras.get("scheduler")
    checks["scheduler"] = "running" if scheduler is not None and scheduler.running else "disabled"
    bot = container.extras.get("bot")
    checks["telegram_bot"] = "running" if bot is not None and bot.running else "disabled"
    checks["ai_provider"] = container.settings.ai_provider
    healthy = checks["storage"] == "ok"
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks}, status_code=200 if healthy else 503
    )
