"""Server-rendered dashboard (Jinja2, no JavaScript framework)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.ai.cover_letter import DOCUMENT_KINDS, GenerationUnavailable
from app.ai.cv_parser import CVError
from app.ai.skills import skill_category
from app.api.deps import (
    DASHBOARD_COOKIE,
    ContainerDep,
    csrf_token,
    dashboard_authenticated,
    get_container,
    session_token,
)
from app.api.profile import read_upload
from app.config import BASE_DIR
from app.database.models import APPLICATION_STATUSES
from app.database.repositories import (
    ApplicationRepository,
    JobFilters,
    JobRepository,
    ProfileRepository,
    SourceRepository,
)
from app.schemas.application import ApplicationUpdate
from app.schemas.match import CATEGORY_LABELS
from app.schemas.profile import ProfileIn, SkillIn
from app.services.application_service import ApplicationNotFound
from app.services.document_service import NotFoundError
from app.services.profile_service import ProfileError

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals.update(category_labels=CATEGORY_LABELS, statuses=APPLICATION_STATUSES, doc_kinds=DOCUMENT_KINDS)

router = APIRouter(include_in_schema=False)


class LoginRequired(Exception):
    pass


def _guard(request: Request) -> None:
    if not dashboard_authenticated(request):
        raise LoginRequired


def _check_csrf(request: Request, token: str) -> None:
    expected = csrf_token(get_container(request).settings.api_key)
    if not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid CSRF token")


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    key = get_container(request).settings.api_key
    return templates.TemplateResponse(request, name, {"csrf": csrf_token(key) if key else "", **ctx})


def _back(url: str, msg: str | None = None) -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + urlencode({"msg": msg})
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


# ------------------------------------------------------------------ auth
@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str | None = None) -> HTMLResponse:
    return render(request, "login.html", error=error)


@router.post("/login")
async def login(request: Request, api_key: Annotated[str, Form()]) -> Response:
    c = get_container(request)
    expected = c.settings.api_key
    if not expected or not secrets.compare_digest(api_key.encode(), expected.encode()):
        return RedirectResponse("/login?error=1", status_code=status.HTTP_303_SEE_OTHER)
    resp = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        DASHBOARD_COOKIE,
        session_token(expected),
        httponly=True,
        samesite="strict",
        secure=c.settings.app_env == "production",
        max_age=60 * 60 * 24 * 14,
    )
    return resp


@router.post("/logout")
async def logout() -> Response:
    resp = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(DASHBOARD_COOKIE)
    return resp


# ------------------------------------------------------------------ jobs
@router.get("/", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    c: ContainerDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    skills: Annotated[str | None, Query(max_length=200)] = None,
    country: Annotated[str | None, Query(max_length=64)] = None,
    remote_type: Annotated[str | None, Query(pattern="^(remote|hybrid|onsite|unknown)?$")] = None,
    min_score: Annotated[str | None, Query(max_length=3)] = None,
    source: Annotated[str | None, Query(max_length=64)] = None,
    sort: Annotated[str, Query(pattern="^(posted_at|score|collected_at)$")] = "score",
    page: Annotated[int, Query(ge=1, le=10_000)] = 1,
    msg: str | None = None,
) -> HTMLResponse:
    _guard(request)
    score = int(min_score) if min_score and min_score.isdigit() else None
    filters = JobFilters(
        q=q or None,
        skills=[s.strip() for s in (skills or "").split(",") if s.strip()][:10],
        country=country or None,
        remote_type=remote_type or None,
        source=source or None,
        min_score=score,
        sort=sort,
    )
    async with c.db.session() as session:
        ref = await ProfileRepository(session).active_ref()
        result = await JobRepository(session).list_jobs(filters, ref, page, 25)
        sources = await SourceRepository(session).list_all()
    params = {
        k: v
        for k, v in dict(
            q=q, skills=skills, country=country, remote_type=remote_type, min_score=min_score, source=source, sort=sort
        ).items()
        if v
    }
    return render(
        request,
        "jobs.html",
        result=result,
        params=params,
        query=urlencode(params),
        sources=sources,
        has_profile=ref is not None,
        msg=msg,
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_page(request: Request, job_id: int, c: ContainerDep, msg: str | None = None) -> HTMLResponse:
    _guard(request)
    async with c.db.session() as session:
        repo = JobRepository(session)
        job = await repo.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
        match = await repo.get_match(job_id, await ProfileRepository(session).active_ref())
        application = await ApplicationRepository(session).get_by_job(job_id)
    docs = await c.services.documents.list_for_job(job_id)
    return render(request, "job_detail.html", job=job, match=match, application=application, docs=docs, msg=msg)


@router.post("/jobs/{job_id}/status")
async def job_status(
    request: Request,
    job_id: int,
    c: ContainerDep,
    csrf: Annotated[str, Form()],
    status_: Annotated[str, Form(alias="status")],
) -> Response:
    _guard(request)
    _check_csrf(request, csrf)
    if status_ not in APPLICATION_STATUSES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid status")
    try:
        await c.services.applications.set_status_for_job(job_id, status_)
    except ApplicationNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _back(f"/jobs/{job_id}", f"Marked as {status_}")


@router.post("/jobs/{job_id}/documents")
async def job_document(
    request: Request, job_id: int, c: ContainerDep, csrf: Annotated[str, Form()], kind: Annotated[str, Form()]
) -> Response:
    _guard(request)
    _check_csrf(request, csrf)
    if kind not in DOCUMENT_KINDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid kind")
    try:
        await c.services.documents.generate(job_id, kind)
    except (GenerationUnavailable, NotFoundError) as exc:
        return _back(f"/jobs/{job_id}", str(exc))
    return _back(f"/jobs/{job_id}", "Document generated (draft — review before sending)")


# ------------------------------------------------------------------ applications
@router.get("/applications", response_class=HTMLResponse)
async def applications_page(
    request: Request, c: ContainerDep, status_filter: str | None = Query(None, alias="status"), msg: str | None = None
) -> HTMLResponse:
    _guard(request)
    statuses = [status_filter] if status_filter in APPLICATION_STATUSES else None
    page = await c.services.applications.list_applications(statuses, 1, 100)
    counts = await c.services.applications.counts()
    return render(request, "applications.html", apps=page.items, counts=counts, current=status_filter, msg=msg)


@router.post("/applications/{app_id}")
async def application_update(
    request: Request,
    app_id: int,
    c: ContainerDep,
    csrf: Annotated[str, Form()],
    status_: Annotated[str, Form(alias="status")],
    notes: Annotated[str, Form()] = "",
    follow_up_at: Annotated[str, Form()] = "",
) -> Response:
    _guard(request)
    _check_csrf(request, csrf)
    try:
        follow = datetime.fromisoformat(follow_up_at).replace(tzinfo=UTC) if follow_up_at else None
        data = ApplicationUpdate(status=status_, notes=notes or None, follow_up_at=follow)  # type: ignore[arg-type]
        await c.services.applications.update(app_id, data)
    except (ValueError, ValidationError) as exc:
        return _back("/applications", f"Invalid input: {str(exc)[:120]}")
    except ApplicationNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return _back("/applications", "Saved")


# ------------------------------------------------------------------ profile
@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request, c: ContainerDep, msg: str | None = None) -> HTMLResponse:
    _guard(request)
    return render(request, "profile.html", profile=await c.services.profiles.get(), msg=msg)


@router.post("/profile")
async def profile_save(
    request: Request,
    c: ContainerDep,
    csrf: Annotated[str, Form()],
    name: Annotated[str, Form()],
    headline: Annotated[str, Form()] = "",
    years_experience: Annotated[float, Form()] = 0,
    target_titles: Annotated[str, Form()] = "",
    preferred_locations: Annotated[str, Form()] = "",
    job_types: Annotated[str, Form()] = "",
    skills: Annotated[str, Form()] = "",
    summary: Annotated[str, Form()] = "",
) -> Response:
    _guard(request)
    _check_csrf(request, csrf)

    def lines(v: str) -> list[str]:
        return [x.strip() for x in v.replace(",", "\n").split("\n") if x.strip()]

    try:
        data = ProfileIn(
            name=name.strip(),
            headline=headline or None,
            years_experience=years_experience,
            target_titles=lines(target_titles),
            preferred_locations=lines(preferred_locations),
            job_types=lines(job_types),
            summary=summary or None,
            skills=[SkillIn(name=s, category=skill_category(s)) for s in lines(skills)],
        )
    except ValidationError as exc:
        return _back("/profile", f"Invalid profile: {exc.errors()[0]['msg']}")
    await c.services.profiles.save_manual(data)
    return _back("/profile", "Profile saved — jobs will be re-matched on the next run")


@router.post("/profile/cv")
async def profile_cv(request: Request, c: ContainerDep, csrf: Annotated[str, Form()], file: UploadFile) -> Response:
    _guard(request)
    _check_csrf(request, csrf)
    try:
        data = await read_upload(file)
        await c.services.profiles.upload_cv(file.filename or "cv.pdf", data)
    except HTTPException as exc:
        return _back("/profile", str(exc.detail))
    except CVError as exc:
        return _back("/profile", f"CV rejected: {exc}")
    return _back("/profile", "CV processed — review the extracted data below")


@router.post("/profile/pending")
async def profile_pending(
    request: Request, c: ContainerDep, csrf: Annotated[str, Form()], action: Annotated[str, Form()]
) -> Response:
    _guard(request)
    _check_csrf(request, csrf)
    try:
        if action == "confirm":
            await c.services.profiles.confirm_pending()
            return _back("/profile", "CV data applied to your profile")
        await c.services.profiles.discard_pending()
        return _back("/profile", "Extracted CV data discarded")
    except ProfileError as exc:
        return _back("/profile", str(exc))


# ------------------------------------------------------------------ sources / stats
@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request, c: ContainerDep) -> HTMLResponse:
    _guard(request)
    async with c.db.session() as session:
        sources = await SourceRepository(session).list_all()
    stats = await c.services.stats.overview(24)
    return render(request, "sources.html", sources=sources, stats=stats)
