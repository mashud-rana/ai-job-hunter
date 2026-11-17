from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.schemas.job import RawJob
from app.utils.hashing import content_hash
from app.utils.http import HttpClient
from app.utils.normalize import (
    InvalidJobError,
    infer_country,
    infer_remote_type,
    is_expired,
    normalize_job,
    parse_salary,
)
from app.utils.text import html_to_text
from app.utils.urls import canonicalize_url, is_valid_public_url


def raw(**kw) -> RawJob:
    base = dict(
        source="test",
        external_id="1",
        title="Senior Laravel Developer",
        company="Acme",
        location="Worldwide Remote",
        apply_url="https://acme.com/jobs/1",
    )
    base.update(kw)
    return RawJob(**base)


class TestUrls:
    def test_canonicalize_strips_tracking_and_normalizes(self):
        a = canonicalize_url("HTTPS://www.Example.com/jobs/12/?utm_source=x&b=2&a=1&trk=abc#frag")
        assert a == "https://example.com/jobs/12?a=1&b=2"

    def test_canonicalize_equivalent_links(self):
        assert canonicalize_url("https://example.com/a/") == canonicalize_url("https://www.example.com/a?ref=li")

    @pytest.mark.parametrize(
        "url,ok",
        [
            ("https://example.com/x", True),
            ("http://jobs.example.org", True),
            ("ftp://example.com", False),
            ("javascript:alert(1)", False),
            ("http://localhost:8000/admin", False),
            ("http://127.0.0.1/", False),
            ("http://10.0.0.5/", False),
            ("http://169.254.169.254/latest/meta-data", False),
            ("http://[::1]/", False),
            ("http://user:pw@example.com/", False),
            ("http://intranet/", False),
            ("http://service.internal/", False),
        ],
    )
    def test_ssrf_guard(self, url, ok):
        assert is_valid_public_url(url) is ok


class TestNormalize:
    def test_basic(self):
        job = normalize_job(raw(description="<p>Build <b>APIs</b></p><script>x()</script>", salary="$80k - $100k"))
        assert job.description == "Build\nAPIs"
        assert job.remote_type == "remote"
        assert job.country == "Worldwide"
        assert (job.salary_min, job.salary_max, job.currency) == (80000, 100000, "USD")
        assert job.canonical_url == "https://acme.com/jobs/1"
        assert len(job.content_hash) == 64

    def test_rejects_invalid(self):
        with pytest.raises(InvalidJobError):
            normalize_job(raw(title=" "))
        with pytest.raises(InvalidJobError):
            normalize_job(raw(apply_url="http://192.168.1.1/x"))

    def test_not_specified_salary(self):
        job = normalize_job(raw(salary="Not specified"))
        assert job.salary_text is None and job.salary_min is None

    def test_future_posted_at_dropped(self):
        job = normalize_job(raw(posted_at=datetime.now(UTC) + timedelta(days=30)))
        assert job.posted_at is None

    def test_bangladesh_onsite(self):
        assert infer_country("Dhaka, Bangladesh") == "Bangladesh"
        assert infer_remote_type("Dhaka", "Senior PHP Developer (On-site)") == "onsite"
        assert infer_remote_type("Hybrid - Dhaka") == "hybrid"

    def test_remote_with_concrete_place_is_not_worldwide(self):
        assert normalize_job(raw(location="Kansas")).country is None
        assert normalize_job(raw(location="Remote")).country == "Worldwide"
        assert normalize_job(raw(location="Düsseldorf", remote_hint="remote")).country == "Germany"

    def test_salary_ignores_years(self):
        assert parse_salary("5+ years, BDT 150,000") == (150000, 150000, "BDT")

    def test_content_hash_insensitive_to_case_and_punctuation(self):
        assert content_hash("Senior PHP Dev!", "ACME Ltd.", "Dhaka") == content_hash(
            "senior php dev", "acme ltd", "dhaka"
        )

    def test_expired(self):
        now = datetime.now(UTC)
        assert is_expired(now - timedelta(days=60), None, 45, now)
        assert is_expired(None, now - timedelta(hours=1), 45, now)
        assert not is_expired(now - timedelta(days=3), None, 45, now)

    def test_html_to_text_plain_passthrough(self):
        assert html_to_text("plain   text") == "plain text"


class TestHttpClient:
    async def test_retries_on_5xx_then_succeeds(self, mock_http):
        route = mock_http.get("https://api.example.com/x").mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json={"ok": True})]
        )
        client = HttpClient(max_retries=3, rate_per_host=0)
        try:
            assert await client.get_json("https://api.example.com/x") == {"ok": True}
        finally:
            await client.aclose()
        assert route.call_count == 2

    async def test_does_not_retry_4xx(self, mock_http):
        route = mock_http.get("https://api.example.com/y").mock(return_value=httpx.Response(404))
        client = HttpClient(max_retries=3, rate_per_host=0)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_json("https://api.example.com/y")
        finally:
            await client.aclose()
        assert route.call_count == 1


def test_log_redaction_of_url_tokens_and_keys():
    import logging

    from app.logging import _redact_value, configure_logging

    assert _redact_value("GET /cron/sync?token=abc&wait=true") == "GET /cron/sync?token=***&wait=true"
    assert "ghp_" not in _redact_value("x ghp_ABCDEFGHIJKLMNOPQRSTUVWX")
    configure_logging("INFO", json_logs=False)
    rec = logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0, '%s - "%s %s"', ("1.2.3.4", "GET", "/cron/sync?token=s3cret"), None
    )
    for f in logging.getLogger("uvicorn.access").filters:
        f.filter(rec)
    assert "s3cret" not in rec.getMessage()
