from __future__ import annotations

import re

import httpx
import pymupdf
import pytest

from app.database.models import JobMatch
from app.database.repositories import JobRepository, ProfileRepository, SourceRepository
from app.main import create_app
from app.schemas.job import RawJob
from app.utils.normalize import normalize_job
from tests.fakes import FakeProvider

H = {"X-API-Key": "test-key"}


@pytest.fixture
async def client(settings, container):
    app = create_app(settings, container)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def seed(container):
    async with container.db.session() as s:
        src = await SourceRepository(s).upsert("remotive", "Remotive", True, None)
        await SourceRepository(s).upsert("facebook", "Facebook", False, "no permitted access")
        await ProfileRepository(s).create(
            {"name": "Md. Test", "years_experience": 5}, [{"name": "PHP"}, {"name": "Laravel"}], True
        )
        ref = await ProfileRepository(s).active_ref()
        specs = [
            ("Senior Laravel Developer", "Remote", "Laravel PHP Redis", 92),
            ("Python Backend Engineer", "Dhaka, Bangladesh", "Python FastAPI PostgreSQL", 74),
            ("Marketing Lead", "Berlin", "campaigns", None),
        ]
        ids = []
        for i, (title, loc, desc, score) in enumerate(specs):
            raw = RawJob(
                source="remotive",
                external_id=str(i),
                title=title,
                company=f"Co{i}",
                location=loc,
                apply_url=f"https://jobs.example.com/{i}",
                description=desc,
            )
            job = await JobRepository(s).insert_if_new(normalize_job(raw), src.id)
            ids.append(job.id)
            if score is not None:
                s.add(
                    JobMatch(
                        job_id=job.id,
                        profile_id=ref.id,
                        profile_version=ref.version,
                        match_score=score,
                        category="excellent" if score >= 90 else "good",
                        matched_skills=["PHP"],
                        missing_skills=[],
                        recommendation="apply",
                        provider="rules",
                        confidence=0.5,
                    )
                )
    return ids


async def test_auth_required(client):
    assert (await client.get("/api/health")).status_code == 200  # public
    for path in (
        "/api/jobs",
        "/api/jobs/top",
        "/api/jobs/stats",
        "/api/matches",
        "/api/applications",
        "/api/profile",
        "/api/sources",
    ):
        r = await client.get(path)
        assert r.status_code == 401, path
    assert (await client.get("/api/jobs", headers={"X-API-Key": "wrong"})).status_code == 401


async def test_api_key_unset_refuses(client, settings):
    settings.api_key = ""
    assert (await client.get("/api/jobs", headers=H)).status_code == 503


async def test_jobs_listing_filters_and_detail(client, container):
    ids = await seed(container)
    r = await client.get("/api/jobs", headers=H, params={"sort": "score"})
    body = r.json()
    assert r.status_code == 200 and body["total"] == 3 and body["pages"] == 1
    assert body["items"][0]["title"] == "Senior Laravel Developer" and body["items"][0]["match_score"] == 92
    assert body["items"][0]["source_key"] == "remotive"

    r = await client.get("/api/jobs", headers=H, params={"skills": "fastapi,postgresql"})
    assert [j["title"] for j in r.json()["items"]] == ["Python Backend Engineer"]
    r = await client.get("/api/jobs", headers=H, params={"country": "Bangladesh"})
    assert [j["title"] for j in r.json()["items"]] == ["Python Backend Engineer"]
    r = await client.get("/api/jobs", headers=H, params={"remote_type": "remote", "min_score": 80})
    assert [j["title"] for j in r.json()["items"]] == ["Senior Laravel Developer"]
    r = await client.get("/api/jobs", headers=H, params={"per_page": 1, "page": 2, "sort": "score"})
    assert r.json()["items"][0]["title"] == "Python Backend Engineer" and r.json()["pages"] == 3
    assert (await client.get("/api/jobs", headers=H, params={"remote_type": "space"})).status_code == 422
    assert (await client.get("/api/jobs", headers=H, params={"per_page": 1000})).status_code == 422

    top = (await client.get("/api/jobs/top", headers=H)).json()
    assert [j["match_score"] for j in top] == [92, 74]

    d = (await client.get(f"/api/jobs/{ids[0]}", headers=H)).json()
    assert d["description"] == "Laravel PHP Redis" and d["match"]["match_score"] == 92
    assert (await client.get("/api/jobs/999999", headers=H)).status_code == 404

    stats = (await client.get("/api/jobs/stats", headers=H)).json()
    assert stats["jobs"]["total_jobs"] == 3 and stats["matches_by_category"]["excellent"] == 1


async def test_matches_and_sources(client, container):
    await seed(container)
    m = (await client.get("/api/matches", headers=H, params={"min_score": 80})).json()
    assert m["total"] == 1 and m["items"][0]["job"]["title"] == "Senior Laravel Developer"
    sources = (await client.get("/api/sources", headers=H)).json()
    fb = next(s for s in sources if s["key"] == "facebook")
    assert fb["available"] is False and fb["unavailable_reason"]
    r = await client.patch("/api/sources/remotive", headers=H, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert (await client.patch("/api/sources/nope", headers=H, json={"enabled": True})).status_code == 404


async def test_application_tracking(client, container):
    ids = await seed(container)
    r = await client.post("/api/applications", headers=H, json={"job_id": ids[0], "status": "interested"})
    assert r.status_code == 201
    app_ = r.json()
    assert app_["position"] == "Senior Laravel Developer" and app_["job_url"] == "https://jobs.example.com/0"
    # same job again -> updates the existing record instead of duplicating
    r2 = await client.post("/api/applications", headers=H, json={"job_id": ids[0], "status": "applied"})
    assert r2.json()["id"] == app_["id"] and r2.json()["applied_at"] is not None

    r = await client.patch(
        f"/api/applications/{app_['id']}",
        headers=H,
        json={"status": "interview", "notes": "Call on Monday", "follow_up_at": "2026-12-01T09:00:00Z"},
    )
    assert r.status_code == 200 and r.json()["status"] == "interview" and r.json()["notes"] == "Call on Monday"

    manual = await client.post(
        "/api/applications",
        headers=H,
        json={"position": "PHP Team Lead", "company": "Local Co", "job_url": "https://local.example.com/x"},
    )
    assert manual.status_code == 201

    assert (await client.post("/api/applications", headers=H, json={"status": "saved"})).status_code == 422
    assert (
        await client.post("/api/applications", headers=H, json={"position": "x", "status": "hired"})
    ).status_code == 422
    assert (await client.post("/api/applications", headers=H, json={"job_id": 99999})).status_code == 404
    assert (await client.patch("/api/applications/99999", headers=H, json={"status": "offer"})).status_code == 404

    listing = (await client.get("/api/applications", headers=H, params={"status": "interview"})).json()
    assert listing["total"] == 1
    assert (await client.get("/api/applications", headers=H)).json()["total"] == 2


def _pdf(text: str) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 72), text, fontsize=10)
    return doc.tobytes()


async def test_profile_endpoints_and_cv_upload(client):
    assert (await client.get("/api/profile", headers=H)).status_code == 404
    body = {
        "name": "Md. Test",
        "years_experience": 5,
        "target_titles": ["Senior PHP Developer"],
        "skills": [{"name": "PHP"}, {"name": "Laravel"}],
    }
    r = await client.put("/api/profile", headers=H, json=body)
    assert r.status_code == 200 and r.json()["is_verified"] and r.json()["version"] == 1
    assert (await client.put("/api/profile", headers=H, json={**body, "years_experience": -1})).status_code == 422

    pdf = _pdf("Jane Doe\nSenior Python Developer\n6 years of experience with Python, FastAPI and Kafka")
    r = await client.post("/api/profile/cv", headers=H, files={"file": ("cv.pdf", pdf, "application/pdf")})
    assert r.status_code == 200 and "Kafka" in {s["name"] for s in r.json()["skills"]}
    prof = (await client.get("/api/profile", headers=H)).json()
    assert prof["name"] == "Md. Test" and prof["pending_profile"]  # verified profile untouched

    bad = await client.post("/api/profile/cv", headers=H, files={"file": ("cv.pdf", b"not a pdf", "application/pdf")})
    assert bad.status_code == 422
    exe = await client.post(
        "/api/profile/cv", headers=H, files={"file": ("x.exe", b"MZ...", "application/x-msdownload")}
    )
    assert exe.status_code == 415

    r = await client.post("/api/profile/confirm", headers=H)
    assert r.status_code == 200 and "Kafka" in {s["name"] for s in r.json()["skills"]}
    assert r.json()["pending_profile"] is None
    assert (await client.post("/api/profile/confirm", headers=H)).status_code == 409


async def test_documents_endpoint(client, container):
    ids = await seed(container)
    r = await client.post(f"/api/jobs/{ids[0]}/documents", headers=H, params={"kind": "cover_letter"})
    assert r.status_code == 503  # no provider configured
    container.services.documents.providers = [FakeProvider(["Dear Co0 team, ..."])]
    r = await client.post(f"/api/jobs/{ids[0]}/documents", headers=H, params={"kind": "cover_letter"})
    assert r.status_code == 201 and r.json()["content"].startswith("Dear")
    assert (await client.post(f"/api/jobs/{ids[0]}/documents", headers=H, params={"kind": "poem"})).status_code == 422
    docs = (await client.get(f"/api/jobs/{ids[0]}/documents", headers=H)).json()
    assert len(docs) == 1


async def test_security_headers(client):
    r = await client.get("/api/health")
    assert (
        r.headers["x-content-type-options"] == "nosniff"
        and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    )


async def test_rate_limit(settings, container):
    settings.rate_limit = "3/minute"
    app = create_app(settings, container)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        codes = [(await c.get("/api/jobs", headers=H)).status_code for _ in range(5)]
        health = (await c.get("/api/health")).status_code  # exempt for monitoring
        limited = await c.get("/api/jobs", headers=H)
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429] and health == 200
    assert int(limited.headers["retry-after"]) >= 1


def test_parse_limit():
    from app.api.ratelimit import parse_limit

    assert parse_limit("60/minute") == (60, 60) and parse_limit("5 / hours") == (5, 3600)
    assert parse_limit("disabled") is None
    with pytest.raises(ValueError):
        parse_limit("lots")


# ------------------------------------------------------------------ dashboard


async def _login(client) -> str:
    r = await client.post("/login", data={"api_key": "test-key"})
    assert r.status_code == 303 and "jh_session" in r.cookies
    client.cookies.set("jh_session", r.cookies["jh_session"])
    page = await client.get("/profile")
    return re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)


async def test_dashboard_requires_login(client):
    r = await client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert (await client.post("/login", data={"api_key": "nope"})).headers["location"] == "/login?error=1"


async def test_dashboard_pages_and_forms(client, container):
    ids = await seed(container)
    csrf = await _login(client)

    page = await client.get("/", params={"min_score": "80"})
    assert page.status_code == 200 and "Senior Laravel Developer" in page.text and "Marketing Lead" not in page.text
    detail = await client.get(f"/jobs/{ids[0]}")
    assert "Laravel PHP Redis" in detail.text and "Excellent Match" in detail.text

    # CSRF enforced
    r = await client.post(f"/jobs/{ids[0]}/status", data={"status": "saved", "csrf": "bad"})
    assert r.status_code == 403
    r = await client.post(f"/jobs/{ids[0]}/status", data={"status": "saved", "csrf": csrf})
    assert r.status_code == 303
    apps = await client.get("/applications")
    assert "Senior Laravel Developer" in apps.text

    app_id = (await client.get("/api/applications", headers=H)).json()["items"][0]["id"]
    r = await client.post(
        f"/applications/{app_id}",
        data={"csrf": csrf, "status": "applied", "notes": "sent", "follow_up_at": "2026-12-01"},
    )
    assert r.status_code == 303
    assert (await client.get("/api/applications", headers=H)).json()["items"][0]["status"] == "applied"

    r = await client.post(
        "/profile",
        data={
            "csrf": csrf,
            "name": "Md. Test",
            "years_experience": "6",
            "skills": "PHP, Laravel, Vue.js",
            "target_titles": "Senior PHP Developer",
        },
    )
    assert r.status_code == 303
    prof = (await client.get("/api/profile", headers=H)).json()
    assert prof["years_experience"] == 6 and {s["name"] for s in prof["skills"]} == {"PHP", "Laravel", "Vue.js"}

    r = await client.post(
        "/profile/cv",
        data={"csrf": csrf},
        files={
            "file": (
                "cv.pdf",
                _pdf("Jane Doe\nSenior PHP Developer\n7 years of experience PHP Kafka"),
                "application/pdf",
            )
        },
    )
    assert r.status_code == 303
    assert "Review data extracted" in (await client.get("/profile")).text
    r = await client.post("/profile/pending", data={"csrf": csrf, "action": "discard"})
    assert (await client.get("/api/profile", headers=H)).json()["pending_profile"] is None

    assert (await client.get("/sources")).status_code == 200


async def test_dashboard_escapes_untrusted_job_text(client, container):
    async with container.db.session() as s:
        src = await SourceRepository(s).upsert("remotive", "Remotive", True, None)
        job = await JobRepository(s).insert_if_new(
            normalize_job(
                RawJob(
                    source="remotive",
                    external_id="x",
                    title="Dev <img src=x onerror=alert(1)>",
                    company="<b>Evil</b>",
                    location="Remote",
                    apply_url="https://jobs.example.com/x",
                    description="plain",
                )
            ),
            src.id,
        )
    await _login(client)
    page = await client.get(f"/jobs/{job.id}")
    assert "<img src=x" not in page.text and "&lt;img src=x" in page.text
