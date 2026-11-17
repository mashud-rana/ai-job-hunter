"""Sync trigger for external cron services (cron-job.org, GitHub Actions, UptimeRobot, ...).

    GET /cron/sync?token=<SYNC_TOKEN>        → 202, runs in the background
    GET /cron/sync?token=...&wait=true       → 200 with a summary (may take a minute)
    Header alternative: X-Sync-Token: <SYNC_TOKEN>  (keeps the token out of URLs)

Each hit collects, matches and alerts; sends the daily digest once a day after DIGEST_HOUR; and runs
maintenance once a day. Hitting it also wakes a sleeping free-tier instance.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse

from app.api.deps import ContainerDep
from app.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/cron", tags=["cron"])

_state: dict[str, asyncio.Task[Any] | None] = {"task": None}


def _check(expected: str, provided: str) -> None:
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "SYNC_TOKEN is not configured")
    if not provided or not secrets.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid sync token")


@router.api_route("/sync", methods=["GET", "POST"])
async def cron_sync(
    c: ContainerDep,
    token: Annotated[str, Query(max_length=200)] = "",
    x_sync_token: Annotated[str, Header(max_length=200)] = "",
    wait: bool = False,
    force: bool = False,
) -> JSONResponse:
    _check(c.settings.sync_token, token or x_sync_token)
    from app.scheduler.tasks import run_due_tasks

    running = _state["task"]
    if running is not None and not running.done():
        return JSONResponse({"status": "already running"}, status_code=status.HTTP_202_ACCEPTED)

    if wait:
        summary = await run_due_tasks(c, force=force)
        return JSONResponse({"status": "done", **summary})

    async def run() -> None:
        try:
            await run_due_tasks(c, force=force)
        except Exception:
            log.exception("cron_sync_failed")

    _state["task"] = asyncio.create_task(run())
    return JSONResponse({"status": "started"}, status_code=status.HTTP_202_ACCEPTED)
