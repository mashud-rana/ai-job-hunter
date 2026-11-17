from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import ContainerDep, PaginationDep, page_meta, require_api_key
from app.schemas.application import ApplicationCreate, ApplicationOut, ApplicationStatus, ApplicationUpdate
from app.schemas.common import Paginated
from app.services.application_service import ApplicationNotFound

router = APIRouter(prefix="/api/applications", tags=["applications"], dependencies=[Depends(require_api_key)])


@router.get("", response_model=Paginated[ApplicationOut])
async def list_applications(
    c: ContainerDep,
    pagination: PaginationDep,
    status_: Annotated[list[ApplicationStatus] | None, Query(alias="status")] = None,
) -> dict[str, Any]:
    page = await c.services.applications.list_applications(
        list(status_) if status_ else None, pagination.page, pagination.per_page
    )
    return {"items": [ApplicationOut.model_validate(a) for a in page.items], **page_meta(page)}


@router.post("", response_model=ApplicationOut, status_code=status.HTTP_201_CREATED)
async def create_application(data: ApplicationCreate, c: ContainerDep) -> ApplicationOut:
    """Track an application. This only records it; nothing is submitted to employers."""
    try:
        return ApplicationOut.model_validate(await c.services.applications.create(data))
    except ApplicationNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.patch("/{app_id}", response_model=ApplicationOut)
async def update_application(app_id: int, data: ApplicationUpdate, c: ContainerDep) -> ApplicationOut:
    try:
        return ApplicationOut.model_validate(await c.services.applications.update(app_id, data))
    except ApplicationNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
