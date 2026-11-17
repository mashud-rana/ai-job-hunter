"""Collector unit tests. All HTTP is mocked with payloads shaped like the real APIs."""

from __future__ import annotations

import html

import httpx
import pytest

from app.collectors.adzuna import AdzunaCollector
from app.collectors.arbeitnow import ArbeitnowCollector
from app.collectors.bdjobs import BDJobsAlertsCollector, parse_bdjobs_alert
from app.collectors.careers import CareerPagesCollector
from app.collectors.email_alerts import AlertEmail, _parse_message
from app.collectors.facebook import FacebookCollector
from app.collectors.linkedin import LinkedInAlertsCollector, parse_linkedin_alert
from app.collectors.remoteok import RemoteOKCollector
from app.collectors.remotive import RemotiveCollector
from app.config import CareerSource
from app.utils.http import HttpClient


@pytest.fixture
async def http():
    client = HttpClient(max_retries=1, rate_per_host=0)
    yield client
    await client.aclose()


REMOTIVE_JOB = {
    "id": 2069746,
    "url": "https://remotive.com/remote-jobs/software-development/senior-laravel-2069746",
    "title": "Senior Laravel Developer",
    "company_name": "Mitre Media",
    "category": "Software Development",
    "tags": ["php", "laravel"],
    "job_type": "full_time",
    "publication_date": "2026-11-14T20:33:53",
    "candidate_required_location": "Worldwide",
    "salary": "$70k - $90k",
    "description": "<p>We use <b>Laravel</b></p>",
}


async def test_remotive(settings, http, mock_http):
    settings.search_keywords = ["laravel", "php"]
    mock_http.get("https://remotive.com/api/remote-jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [REMOTIVE_JOB, {"id": None}]})
    )
    jobs = await RemotiveCollector(settings, http).collect()
    assert len(jobs) == 1  # deduped across both keyword queries, invalid skipped
    j = jobs[0]
    assert j.external_id == "2069746" and j.job_type == "Full-time" and j.remote_hint == "remote"
    assert j.posted_at.year == 2026


async def test_remotive_all_queries_fail(settings, http, mock_http):
    settings.search_keywords = ["laravel"]
    mock_http.get("https://remotive.com/api/remote-jobs").mock(return_value=httpx.Response(500))
    with pytest.raises(RuntimeError, match="all Remotive queries failed"):
        await RemotiveCollector(settings, http).collect()


async def test_remoteok(settings, http, mock_http):
    payload = [
        {"last_updated": 1, "legal": "API Terms of Service"},
        {
            "id": "1137796",
            "epoch": 1794556821,
            "date": "2026-11-13T08:00:21+00:00",
            "company": "Fuku",
            "position": "Senior PHP Engineer",
            "tags": ["php"],
            "description": "<p>PHP</p>",
            "location": "",
            "salary_min": 60000,
            "salary_max": 90000,
            "url": "https://remoteOK.com/remote-jobs/x-1137796",
        },
        {"id": "2", "position": "No URL"},
    ]
    mock_http.get("https://remoteok.com/api").mock(return_value=httpx.Response(200, json=payload))
    jobs = await RemoteOKCollector(settings, http).collect()
    assert len(jobs) == 1
    assert jobs[0].salary_min == 60000 and jobs[0].salary == "$60,000 - $90,000"
    assert jobs[0].location == "Remote"


async def test_arbeitnow_paginates(settings, http, mock_http):
    item = {
        "slug": "backend-berlin-1",
        "company_name": "Augustus",
        "title": "Backend Engineer (Python)",
        "description": "<p>FastAPI</p>",
        "remote": True,
        "url": "https://www.arbeitnow.com/jobs/x",
        "tags": ["Engineering"],
        "job_types": ["Full Time"],
        "location": "Berlin",
        "created_at": 1794887251,
    }
    route = mock_http.get("https://www.arbeitnow.com/api/job-board-api").mock(
        side_effect=[
            httpx.Response(200, json={"data": [item], "links": {"next": "page2"}}),
            httpx.Response(200, json={"data": [{**item, "slug": "b2", "remote": False}], "links": {"next": None}}),
        ]
    )
    jobs = await ArbeitnowCollector(settings, http).collect()
    assert [j.external_id for j in jobs] == ["backend-berlin-1", "b2"]
    assert route.call_count == 2 and jobs[0].remote_hint == "remote" and jobs[0].job_type == "Full Time"
    assert jobs[1].remote_hint == "onsite"


async def test_adzuna_requires_credentials(settings, http):
    av = AdzunaCollector(settings, http).availability()
    assert not av.available and "ADZUNA" in av.reason


async def test_adzuna(settings, http, mock_http):
    settings.adzuna_app_id, settings.adzuna_app_key = "id", "key"
    settings.adzuna_countries, settings.adzuna_queries = ["gb"], ["laravel"]
    mock_http.get("https://api.adzuna.com/v1/api/jobs/gb/search/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "555",
                        "title": "Laravel Developer",
                        "description": "PHP...",
                        "created": "2026-11-10T10:00:00Z",
                        "redirect_url": "https://www.adzuna.co.uk/jobs/land/ad/555",
                        "company": {"display_name": "Acme"},
                        "location": {"display_name": "London, UK"},
                        "salary_min": 50000,
                        "salary_max": 65000,
                        "contract_time": "full_time",
                        "category": {"label": "IT Jobs"},
                    }
                ]
            },
        )
    )
    jobs = await AdzunaCollector(settings, http).collect()
    assert jobs[0].currency == "GBP" and jobs[0].job_type == "Full-time" and jobs[0].company == "Acme"


async def test_career_pages(settings, http, mock_http):
    settings.career_sources = [
        CareerSource(kind="greenhouse", identifier="gitlab"),
        CareerSource(kind="lever", identifier="acme", company="Acme"),
        CareerSource(kind="ashby", identifier="ashby"),
        CareerSource(kind="rss", identifier="http://127.0.0.1/feed"),  # SSRF: must be rejected
    ]
    mock_http.get("https://boards-api.greenhouse.io/v1/boards/gitlab/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 1,
                        "title": "Backend Engineer",
                        "absolute_url": "https://job-boards.greenhouse.io/gitlab/jobs/1",
                        "location": {"name": "Remote, EMEA"},
                        "company_name": "GitLab",
                        "content": html.escape("<p>Ruby & Go</p>"),
                        "first_published": "2026-11-01T10:00:00-04:00",
                    }
                ]
            },
        )
    )
    mock_http.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": "abc",
                    "text": "Senior PHP Engineer",
                    "hostedUrl": "https://jobs.lever.co/acme/abc",
                    "categories": {"location": "Dhaka", "commitment": "Full-time"},
                    "descriptionPlain": "Laravel",
                    "createdAt": 1788199939703,
                    "workplaceType": "hybrid",
                    "salaryRange": {"min": 1000, "max": 2000, "currency": "USD"},
                }
            ],
        )
    )
    mock_http.get("https://api.ashbyhq.com/posting-api/job-board/ashby").mock(
        return_value=httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "u1",
                        "title": "Engineering Manager",
                        "location": "Remote - EU",
                        "isRemote": True,
                        "jobUrl": "https://jobs.ashbyhq.com/Ashby/u1",
                        "descriptionPlain": "Lead",
                        "isListed": True,
                        "publishedAt": "2026-03-04T14:29:08.532+00:00",
                        "compensation": {"compensationTierSummary": "€150K – €250K"},
                    }
                ]
            },
        )
    )
    jobs = await CareerPagesCollector(settings, http).collect()
    by_title = {j.title: j for j in jobs}
    assert set(by_title) == {"Backend Engineer", "Senior PHP Engineer", "Engineering Manager"}
    assert by_title["Backend Engineer"].description == "<p>Ruby & Go</p>"
    assert by_title["Senior PHP Engineer"].remote_hint == "hybrid"
    assert by_title["Senior PHP Engineer"].posted_at.year == 2026
    assert by_title["Engineering Manager"].salary == "€150K – €250K"
    assert by_title["Backend Engineer"].external_id == "greenhouse:gitlab:1"


async def test_career_rss(settings, http, mock_http):
    rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>Acme Careers</title>
      <item><title>Python Backend Developer</title><link>https://acme.example.com/jobs/7</link>
      <guid>job-7</guid><description>FastAPI and PostgreSQL</description>
      <pubDate>Mon, 16 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    jobs = CareerPagesCollector(settings, http).parse_rss(rss, CareerSource(kind="rss", identifier="u"))
    assert jobs[0].title == "Python Backend Developer" and jobs[0].company == "Acme Careers"


def test_facebook_unavailable(settings):
    av = FacebookCollector(settings, None).availability()  # type: ignore[arg-type]
    assert av.available is False and "Terms" in av.reason


LINKEDIN_HTML = """
<html><body><table>
 <tr><td><table><tr><td>
   <a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc&refId=x">Senior Laravel Developer</a>
   <p>Acme Ltd · Dhaka, Bangladesh (Hybrid)</p><p>Actively recruiting</p>
 </td></tr></table></td></tr>
 <tr><td><table><tr><td>
   <a href="https://www.linkedin.com/comm/jobs/view/4012345679/"><img alt="logo"></a>
   <a href="https://www.linkedin.com/comm/jobs/view/4012345679/">Python Backend Engineer</a>
   <div>Globex</div><div>Remote</div>
 </td></tr></table></td></tr>
 <tr><td><a href="https://www.linkedin.com/comm/jobs/search?keywords=php">See all jobs</a></td></tr>
</table></body></html>
"""


def test_linkedin_alert_parser():
    jobs = parse_linkedin_alert(LINKEDIN_HTML)
    by_id = {j.external_id: j for j in jobs}
    assert set(by_id) == {"4012345678", "4012345679"}
    a = by_id["4012345678"]
    assert (a.title, a.company, a.location) == (
        "Senior Laravel Developer",
        "Acme Ltd",
        "Dhaka, Bangladesh (Hybrid)",
    )
    assert a.apply_url == "https://www.linkedin.com/jobs/view/4012345678/"
    b = by_id["4012345679"]
    assert (b.company, b.location) == ("Globex", "Remote")


BDJOBS_HTML = """
<table>
<tr><td><a href="https://jobs.bdjobs.com/jobdetails.asp?id=1234567&ln=1">Senior PHP Developer (Laravel)</a><br>
 Tech Solutions Ltd.<br>Location: Dhaka<br>Deadline: 30 Nov 2026</td></tr>
<tr><td><a href="https://bdjobs.com/h/details/1234999">Software Engineer</a><br>Brain Station</td></tr>
<tr><td><a href="https://evil.example.com/jobdetails.asp?id=999999">Phishing</a></td></tr>
<tr><td><a href="https://jobs.bdjobs.com/jobdetails.asp?id=1234567">View Details</a></td></tr>
</table>
"""


def test_bdjobs_alert_parser():
    jobs = {j.external_id: j for j in parse_bdjobs_alert(BDJOBS_HTML)}
    assert set(jobs) == {"1234567", "1234999"}
    j = jobs["1234567"]
    assert j.company == "Tech Solutions Ltd." and j.location == "Dhaka"
    assert j.expires_at and j.expires_at.month == 11
    assert jobs["1234999"].location == "Bangladesh"


def test_email_message_parsing():
    raw = (
        b"From: LinkedIn <jobalerts-noreply@linkedin.com>\r\nSubject: =?utf-8?q?New_jobs?=\r\n"
        b"Date: Mon, 16 Nov 2026 10:00:00 +0000\r\nMessage-ID: <m1@x>\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="b"\r\n\r\n'
        b"--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nplain\r\n"
        b"--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<a href='x'>html</a>\r\n--b--\r\n"
    )
    msg = _parse_message(raw)
    assert msg.subject == "New jobs" and "html" in msg.html and msg.text.strip() == "plain"
    assert msg.received_at.year == 2026


def test_email_collectors_availability_and_parse(settings):
    li = LinkedInAlertsCollector(settings, None)  # type: ignore[arg-type]
    assert not li.availability().available
    settings.imap_host, settings.imap_user, settings.imap_password = "imap.example.com", "u", "p"
    assert li.availability().available
    alert = AlertEmail("id", "s", "from", None, LINKEDIN_HTML, "")
    assert len(li.parse_email(alert)) == 2
    bd = BDJobsAlertsCollector(settings, None)  # type: ignore[arg-type]
    assert len(bd.parse_email(AlertEmail("id", "s", "f", None, BDJOBS_HTML, ""))) == 2


BDJOBSTODAY_HTML = """
<table>
<tr><td height="60" width="8"> </td>
<td colspan="2" valign="middle"><span class="STYLE10"><strong>
  <a class="link02" href="../job_details.php?id=293302" target="_blank">Programmer</a></strong></span><br/>
  <span class="HotJobsCompany">Bangladesh Public Service Commission -BPSC</span></td>
<td align="left" class="details" width="165">Deadline: <strong>30 Nov, 2026</strong><br/>
  <span class="detailsbox">Posting: 12 Nov, 2026</span></td></tr>
<tr><td></td><td colspan="2"><a class="link02" href="../job_details.php?id=294016">Assistant Teacher (ICT)</a><br/>
  <span class="HotJobsCompany">Some School</span></td><td class="details">Deadline: <strong>25 Nov, 2026</strong></td></tr>
<tr><td></td><td colspan="2"><a class="link02" href="../job_details.php?id=293764">Liftman</a><br/>
  <span class="HotJobsCompany">University of Dhaka</span></td><td class="details">Deadline: <strong>01 Dec, 2026</strong></td></tr>
</table>
<div class="STYLE10"><a href="hotjobs_details.php?id=293302">Programmer</a></div>
"""


def test_bdjobstoday_parse_listing():
    from app.collectors.bdjobstoday import parse_listing

    jobs = {
        j.external_id: j
        for j in parse_listing(BDJOBSTODAY_HTML, "https://www.bdjobstoday.com/jobsbycategory.php?cat=3")
    }
    assert set(jobs) == {"293302", "294016", "293764"}  # duplicate sidebar link collapsed
    j = jobs["293302"]
    assert j.company == "Bangladesh Public Service Commission -BPSC"
    assert j.apply_url == "https://www.bdjobstoday.com/job_details.php?id=293302"
    assert j.expires_at.day == 30 and j.posted_at.day == 12 and j.location == "Bangladesh"


async def test_bdjobstoday_collector_filters(settings, http, mock_http, db):
    from app.collectors.bdjobstoday import BDJobsTodayCollector
    from app.services.collection_service import CollectionService

    settings.bdjobstoday_categories = [3]
    route = mock_http.get("https://www.bdjobstoday.com/jobsbycategory.php").mock(
        return_value=httpx.Response(200, text=BDJOBSTODAY_HTML)
    )
    collector = BDJobsTodayCollector(settings, http)
    raws = await collector.collect()
    assert [j.title for j in raws] == ["Programmer", "Liftman"]  # teacher post excluded
    assert route.calls[0].request.url.params["cat"] == "3"

    summary = await CollectionService(db, settings, [collector]).run(force=True)
    r = summary.results[0]
    assert r.new == 1 and r.irrelevant == 1  # Liftman dropped by the developer-role keyword filter
