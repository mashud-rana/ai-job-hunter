from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status

from app.ai.cv_parser import MAX_PDF_BYTES, CVError
from app.api.deps import ContainerDep, require_api_key
from app.schemas.profile import ProfileExtraction, ProfileIn, ProfileOut
from app.services.profile_service import ProfileError

router = APIRouter(prefix="/api/profile", tags=["profile"], dependencies=[Depends(require_api_key)])

ALLOWED_TYPES = {"application/pdf", "application/x-pdf", "application/octet-stream"}


@router.get("", response_model=ProfileOut)
async def get_profile(c: ContainerDep) -> ProfileOut:
    profile = await c.services.profiles.get()
    if profile is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no candidate profile yet")
    return ProfileOut.model_validate(profile)


@router.put("", response_model=ProfileOut)
async def put_profile(data: ProfileIn, c: ContainerDep) -> ProfileOut:
    """Manual edit; marks the profile verified and triggers re-matching on the next run."""
    return ProfileOut.model_validate(await c.services.profiles.save_manual(data))


async def read_upload(file: UploadFile) -> bytes:
    if file.content_type not in ALLOWED_TYPES or not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "only PDF files are accepted")
    data = await file.read(MAX_PDF_BYTES + 1)  # bounded read
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file too large (max 5 MB)")
    return data


@router.post("/cv", response_model=ProfileExtraction)
async def upload_cv(file: UploadFile, c: ContainerDep) -> ProfileExtraction:
    """Extract profile data from a PDF CV. A verified profile is NOT changed until /confirm is called."""
    data = await read_upload(file)
    try:
        _, extraction = await c.services.profiles.upload_cv(file.filename or "cv.pdf", data)
    except CVError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    return extraction


@router.post("/confirm", response_model=ProfileOut)
async def confirm_pending(c: ContainerDep, merge_skills: bool = True) -> ProfileOut:
    try:
        return ProfileOut.model_validate(await c.services.profiles.confirm_pending(merge_skills))
    except ProfileError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.delete("/pending", response_model=ProfileOut)
async def discard_pending(c: ContainerDep) -> ProfileOut:
    try:
        return ProfileOut.model_validate(await c.services.profiles.discard_pending())
    except ProfileError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
