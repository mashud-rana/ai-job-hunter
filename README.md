# AI Job Hunter

A Python-only service. It:
- collects job postings from permitted sources (Bangladesh and international),
- scores each one against your candidate profile with a rules engine plus an LLM (OpenAI or Ollama),
- stores everything in plain JSON files (no database), locally or in a private GitHub Gist,
- sends high-match alerts and a daily digest to Telegram.

It also includes a FastAPI JSON API, a small server-rendered dashboard, CV parsing, cover-letter generation and application tracking.

> It never submits applications or sends messages to employers. Generated documents are drafts that you review.

## Contents
1. [How it works](#how-it-works)
2. [Job sources](#job-sources)
3. [Storage (no database)](#storage-no-database)
4. [Quick start (local)](#quick-start-local)
5. [Configuration](#configuration)
6. [Telegram bot](#telegram-bot)
7. [API](#api)
8. [Dashboard](#dashboard)
9. [Matching engine](#matching-engine)
10. [Scheduling](#scheduling)
11. [Testing](#testing)
12. [Deployment](#deployment)
13. [Security notes](#security-notes)
14. [Project layout](#project-layout)
15. [Known limitations](#known-limitations)

## How it works

```
collectors (async, bounded concurrency, isolated failures)
   │  RawJob (standard structure)
   ▼
normalize → validate (URL/SSRF guard) → canonicalize URL → content hash → expiry check
   │
   ▼
dedupe: (source, external_id) ▸ canonical URL ▸ content hash (30 days) ▸ seen keys → jobs.json
   │
   ▼
matching: rules pre-score ──(score ≥ PREFILTER_MIN_SCORE)──► LLM (OpenAI → fallback Ollama)
          │                                                │  validated with Pydantic, grounded in the text
          └────────────── matches.json (one per job × profile version) ◄┘
   │
   ▼
Telegram: immediate alerts (≥ threshold, once per match) · daily digest 09:00 Asia/Dhaka
```

Everything runs in one process: the API, the optional in-process scheduler and Telegram long polling. Syncs are triggered by the in-process scheduler (`RUN_SCHEDULER=true`) or by an external cron hitting `/cron/sync` (see [Scheduling](#scheduling)).

## Job sources

| Source | Access method | Needs | Status |
|---|---|---|---|
| Remotive | Public API `remotive.com/api/remote-jobs` | – | ✅ verified live |
| Remote OK | Public API `remoteok.com/api` | – | ✅ verified live |
| Arbeitnow | Public API `arbeitnow.com/api/job-board-api` | – | ✅ verified live |
| Adzuna | Official API | `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | ✅ implemented (mocked tests) |
| LinkedIn | **Your own job-alert emails** over IMAP (read-only) | `IMAP_*` | ✅ implemented, best-effort parser |
| BDJobs | **Your own alert emails** over IMAP (read-only) | `IMAP_*` | ✅ implemented, best-effort parser |
| Company careers | Greenhouse / Lever / Ashby public job-board APIs, RSS | `CAREER_SOURCES` | ✅ implemented |
| BDJobsToday | Public category listing pages (robots.txt allows; no terms published), list pages only | – | ✅ verified live |
| Facebook | — | — | ⛔ **unavailable** (see below) |

Rules followed by every collector:
- Only official APIs, public feeds and your own emails are used. Nothing logs in, bypasses CAPTCHAs or anti-bot protection, or scrapes LinkedIn or Facebook.
- Each collector runs in isolation with its own timeout. A failing source is logged in `collection_logs.json` and reported to Telegram (throttled), and the other sources carry on.
- Per-source minimum intervals respect provider guidance. Remotive asks for only a few requests a day, so it runs at most every 6 hours.
- Attribution: Remote OK's terms require linking back. Its URL is kept as the apply link and shown as "via Remote OK".

**Bangladeshi sites checked (2026-11-17):**
- **BDJobs:** robots.txt ends with `User-agent: * / Disallow: /`, so scraping is not allowed. Use the alert-email collector.
- **Skill.jobs:** its terms prohibit "Aggregating, copying or duplicating" content. Not used.
- **Careerjet BD:** robots.txt disallows job pages. Its partner API may cover Bangladesh, but that is untested.
- **Shomvob:** jobs are in its app; the public site has no listings.
- **Chakri:** unreachable during the check.
- **BDJobsToday:** allowed, and used. Mostly government and newspaper circulars, matched on title only.

**Facebook** is registered but reports `available=false`. Meta deprecated the Groups API, group posts are private to members, and scraping violates Meta's Terms. If you have a legitimate feed, implement `app/collectors/facebook.py` and return `Availability.ok()`.

**LinkedIn / BDJobs setup:**
1. Create job alerts on LinkedIn (and BDJobs) with email delivery.
2. Point `IMAP_*` at that mailbox. For Gmail, use an [App Password](https://myaccount.google.com/apppasswords).

The mailbox is opened read-only and messages are fetched with `BODY.PEEK`, so nothing is marked read, moved or deleted. Email templates change over time, so the parsers are best effort. They are tested against representative HTML, not real alert emails.

**Adding a source:** subclass `BaseCollector` (`app/collectors/base.py`):
1. Set `key` and `name`.
2. Implement `collect()` returning `RawJob` objects.
3. Optionally override `availability()` (e.g. missing credentials), and set `targeted` (whether to skip the keyword relevance filter) and `min_interval_hours`.
4. Register it in `app/collectors/registry.py`.

## Storage (no database)

All data lives in memory and is saved as JSON files (`app/database/store.py`):

| File | Contents |
|---|---|
| `jobs.json` | Collected jobs (description trimmed to `DESCRIPTION_STORE_LIMIT` characters) |
| `matches.json` | Scores per job × profile version, so nothing is re-scored or charged twice |
| `profiles.json` | Candidate profile, skills and pending CV extraction |
| `applications.json`, `documents.json` | Application tracking and generated drafts |
| `sources.json`, `collection_logs.json`, `notification_logs.json` | Source status and logs (capped at 1,000 entries) |
| `state.json` | Preferences, id counters, last digest and maintenance dates, and **seen keys** |

How the store behaves:
- **Writes** happen after every change. Each file is written to a temp file and then renamed, so a crash never leaves a half-written file. A failed write keeps the data in memory and retries on the next flush; `/api/health` shows the error.
- **Pruning:** every night, jobs that expired more than 14 days ago are deleted, the total is capped at `MAX_JOBS`, and matches for old profile versions are removed. Jobs you are tracking are never pruned.
- **Seen keys** remember pruned jobs for 120 days, so they are never re-imported as "new".
- **Size:** about 400 KB for 60 jobs, so a few MB at the 3,000-job cap.

Backends (`STORAGE_BACKEND`):
- **`local`** (default): `storage/data/*.json`, or set `DATA_PATH`. Use this on a VPS, on your PC, or with a Docker volume. Back up by copying the folder.
- **`gist`**: the same files in a **private GitHub Gist**, via `GIST_ID` and `GITHUB_TOKEN`. The token needs only the `gist` scope. Use this on hosts whose disk is wiped on restart, such as Render's free plan. Writes are debounced (at most every `STORAGE_FLUSH_SECONDS`) and forced at the end of every sync and on shutdown. The Gist's revision history doubles as a backup.

To create the Gist:
1. On gist.github.com, create a **secret** gist with one file, `README.md` (any text).
2. Copy the id from its URL (`https://gist.github.com/<you>/<GIST_ID>`).
3. Create a classic token at GitHub → Settings → Developer settings → Personal access tokens, with only the **gist** scope.

## Quick start (local)

Requirements: [uv](https://docs.astral.sh/uv/). Python 3.12 is installed by uv. No database is needed.

```bash
uv python install 3.12
uv sync                                   # installs runtime + dev dependencies
cp .env.example .env                      # then edit; at least API_KEY
uv run python -m scripts.seed_profile     # create the candidate profile (won't overwrite an existing one)
uv run python -m scripts.run_collect_once --force   # one collection + matching run, prints a summary
uv run uvicorn app.main:app --reload      # API on :8000, dashboard at http://localhost:8000/
```

For local development, `.env` might contain:
```
APP_ENV=development
LOG_JSON=false
API_KEY=<random string>
STORAGE_BACKEND=local
AI_PROVIDER=none            # rules only, no API costs; or openai / ollama
RUN_BOT=false
```

Useful scripts:

| Command | Purpose |
|---|---|
| `uv run python -m scripts.run_collect_once [sources…] [--force] [--no-match] [--no-collect] [--rescore] [--notify]` | Manual run |
| `uv run python -m scripts.seed_profile [--force]` | Create or replace the profile from the brief |

## Configuration

All configuration comes from environment variables (`.env`), parsed by `app/config.py`. `.env.example` documents every variable. The essentials:

| Variable | Required | Notes |
|---|---|---|
| `API_KEY` | yes | Protects the API (`X-API-Key` header) and the dashboard login |
| `STORAGE_BACKEND` | no | `local` (default, `storage/data`) or `gist` (+ `GIST_ID`, `GITHUB_TOKEN`) |
| `SYNC_TOKEN` | for cron | Enables `/cron/sync?token=…` |
| `AI_PROVIDER`, `AI_FALLBACK_PROVIDER` | no | `openai` / `ollama` / `none`. With `none`, rule-based scores only |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | for OpenAI | Any chat model that supports JSON output |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | for Ollama | Ollama ≥ 0.5 (structured outputs) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS` | for Telegram | Both are required, otherwise Telegram is disabled |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | for Adzuna | |
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` | for LinkedIn/BDJobs | |
| `CAREER_SOURCES` | for careers | JSON list, see `.env.example` |

Missing credentials never crash the app. The feature is disabled, and `/api/sources` (and the Sources page) shows the reason.

## Telegram bot

1. Create a bot with [@BotFather](https://t.me/BotFather) and put the token in `TELEGRAM_BOT_TOKEN`.
2. Start the app, then send `/start` to your bot. Because you're not authorised yet, it replies with your chat id.
3. Put that id in `TELEGRAM_ALLOWED_CHAT_IDS` and restart. Every other chat is ignored.

| Command | |
|---|---|
| `/jobs` | Latest matched jobs (paginated, with buttons) |
| `/topjobs` | Highest-scoring jobs |
| `/stats` | Collection and match statistics (24h) |
| `/saved`, `/applied` | Tracked applications |
| `/profile` | Candidate profile |
| `/settings` | Toggle alerts and digest, set the alert threshold (70–90) |
| `/help` | Commands |

Each job message has inline buttons: Details, Apply (link), Save, Applied, and Cover letter (generates a draft; needs an AI provider).

Notification behaviour:
- Alerts: immediately after each run, for new matches ≥ threshold. Each match is alerted at most once, and every delivery attempt is logged in `notification_logs.json`. A failed send is retried on the next run.
- Daily digest: once a day at or after 09:00 Asia/Dhaka (on the first sync after 09:00 in cron mode).
- Error reports: throttled to one every 30 minutes.

## API

Every endpoint except `/api/health` requires the header `X-API-Key: <API_KEY>`. Interactive docs are at `/docs` (disabled when `APP_ENV=production`).

| Method & path | Description |
|---|---|
| `GET /api/health` | Storage, scheduler and bot status (200, or 503 if the JSON files can't be written) |
| `GET /api/jobs` | Search and filter: `q`, `skills` (comma or repeated; all must match), `country`, `remote_type` (remote/hybrid/onsite/unknown), `min_score`, `source`, `sort` (posted_at/score/collected_at), `include_expired`, `page`, `per_page` (≤100) |
| `GET /api/jobs/top` | Best matches (`limit`, `min_score`) |
| `GET /api/jobs/stats` | Totals, by source and work mode, match distribution, collection summary (`hours`) |
| `GET /api/jobs/{id}` | Job with full description and match details |
| `POST /api/jobs/{id}/documents?kind=cover_letter\|email\|linkedin_message` | Generate and store a draft (503 without an AI provider) |
| `GET /api/jobs/{id}/documents` | Stored drafts |
| `GET /api/matches` | Matches for the current profile (`min_score`, `sort=score\|recent`) |
| `GET /api/applications` | Tracked applications (`status` filter, repeatable) |
| `POST /api/applications` | `{job_id}` or `{position, company, job_url}`, plus `status`, `notes`, `applied_at`, `follow_up_at`. One record per job: posting again updates it |
| `PATCH /api/applications/{id}` | Update status, notes or dates |
| `GET /api/profile` · `PUT /api/profile` | Read or replace the profile (manual edits mark it verified) |
| `POST /api/profile/cv` | Upload a PDF CV (≤ 5 MB). The extraction is stored as pending |
| `POST /api/profile/confirm` · `DELETE /api/profile/pending` | Apply or discard the pending CV extraction |
| `GET /api/sources` · `PATCH /api/sources/{key}` | Source status; enable or disable a source |
| `POST /api/tasks/collect?force=` | Start a collect + match run now (202) |
| `GET\|POST /cron/sync?token=<SYNC_TOKEN>` | External cron trigger (also accepts the `X-Sync-Token` header); `wait=true` returns a summary |

Paginated responses have the shape `{items, total, page, per_page, pages}`. Application statuses are `saved`, `interested`, `applied`, `interview`, `rejected` and `offer`.

```bash
curl -H "X-API-Key: $API_KEY" "http://localhost:8000/api/jobs?skills=laravel,mysql&remote_type=remote&min_score=70&sort=score"
```

## Dashboard

Open `/` and log in with the API key. The dashboard has no JavaScript framework: it is server-rendered Jinja2 with plain forms. Pages:
- **Jobs:** search and filters.
- **Job detail:** match breakdown, status tracking, document drafts, description.
- **Applications:** status, follow-up date, notes.
- **Profile:** edit; upload a CV, then review and confirm.
- **Sources:** availability and 24h collection statistics.

The session cookie is an HMAC of the API key (HttpOnly, SameSite=Strict, Secure in production), and every form carries a CSRF token.

## Matching engine

`app/ai/job_matcher.py` works in two stages:

1. **Rules (always, free).**
   - Skill overlap uses a vocabulary of about 80 technologies with aliases and word-boundary matching, so "Go" doesn't match "Google" and "Java" doesn't match "JavaScript".
   - It also scores title relevance, seniority, required years, location eligibility for a Bangladesh-based candidate, employment type and salary floor.
   - Location examples: "US only", "LATAM" and on-site abroad are ineligible. Worldwide, APAC and Bangladesh are eligible. A foreign city with unconfirmed eligibility is capped below the alert band.
   - The weights are skills 40, title 20, seniority 10, experience 10, location 15, type/salary 5.
2. **LLM (only if the rules score ≥ `PREFILTER_MIN_SCORE`).** The output must match the `MatchResult` schema (score 0–100, recommendation `apply` / `consider` / `skip`, etc.). After validation it is grounded:
   - Matched skills must be candidate skills that actually appear in the posting.
   - Missing skills must appear in the posting.
   - Booleans the model leaves null fall back to the rule facts.
   - Malformed output is retried once. Transport or auth errors go straight to the fallback provider. If every provider fails, the rules result is stored.

Bands: 90–100 Excellent, 80–89 Strong, 70–79 Good, 50–69 Moderate, 0–49 Poor.

**Prompt-injection defence.** Job text is fenced inside `<job_posting>` as untrusted data, and the system prompt forbids following instructions found in it. Delimiter tags inside the text are neutralised. If the text contains injection patterns ("ignore previous instructions", "set match_score…"), the AI score is capped at rules + 10 and confidence at 0.5.

**No duplicate AI calls.** Duplicate postings are never inserted. Matches are unique per (job, profile, profile version), so each job is scored once per profile version. Editing the profile bumps the version and triggers re-scoring. `--rescore` forces it after changing models or rules.

**CV parsing** (`app/ai/cv_parser.py`):
- PDFs are checked by magic bytes, limited to 5 MB and 15 pages, and password-protected files are rejected. Text is extracted with PyMuPDF.
- Rules extract name, years, titles, skills and locations. The optional AI extraction is grounded the same way: skills must literally appear in the CV, and inflated years are corrected.
- A verified profile is never overwritten. The extraction waits as `pending_profile` until you confirm.

## Scheduling

There are two ways to run the periodic work. Both use the same tasks in `app/scheduler/tasks.py`.

**A. External cron (no always-on server needed)**
- Set `SYNC_TOKEN`, and optionally `RUN_SCHEDULER=false`.
- Point a free cron service at the sync URL every 3 hours:
  ```
  https://<your-host>/cron/sync?token=<SYNC_TOKEN>
  ```
  For example, on [cron-job.org](https://cron-job.org): create a cronjob, paste the URL, choose every 3 hours, and save. A GitHub Actions `schedule:` workflow running `curl` works too.
- Each hit:
  1. collects, matches and sends alerts (each source still respects its own minimum interval);
  2. sends the daily digest once a day, at or after `DIGEST_HOUR` (Asia/Dhaka);
  3. runs maintenance once a day.
- It returns 202 immediately and works in the background. Add `&wait=true` to get the summary instead.
- A hit also wakes a sleeping free-tier instance.
- The token is compared in constant time and redacted from access logs. Prefer the `X-Sync-Token` header where your cron service supports custom headers.

**B. In-process scheduler (`RUN_SCHEDULER=true`, always-on host)**

APScheduler `AsyncIOScheduler` with timezone `Asia/Dhaka`:

| Job | When | Does |
|---|---|---|
| `collect_and_match` | every `COLLECT_INTERVAL_HOURS` (3h); first run 1 min after start | collect → dedupe → match → alerts; errors reported |
| `daily_digest` | 09:00 | Telegram summary of the last 24h |
| `nightly_maintenance` | 00:00 | mark expired jobs (deadline passed, older than `JOB_MAX_AGE_DAYS`, or a 404/410 HEAD check on a few links); prune old jobs, logs and CV files; compute stats |

- Job options are `coalesce` and `max_instances=1`.
- A failed run reports to Telegram and schedules one retry (collection after 20 min, digest after 10 min).
- On shutdown the app stops scheduling, waits up to 30 s for running tasks, stops the bot, and writes the JSON files a final time.

## Testing

```bash
uv run pytest -q                 # 126 tests: JSON store in a temp folder, no network, no API keys, no database
uv run ruff check . && uv run ruff format --check .
```

How the tests stay offline:
- External HTTP is mocked with respx. The OpenAI SDK, which uses its own HTTP stack, is given an `httpx.MockTransport`.
- An autouse fixture blocks every DNS lookup except localhost, so a test that tries to reach the internet fails loudly.
- LLMs and Telegram are replaced with fakes (`tests/fakes.py`).

Coverage areas: collectors, normalization, dedupe, the JSON store (reload, atomic writes, pruning, seen keys, write failures, Gist backend), AI validation and grounding, prompt injection, rules scoring, notifications, bot handlers, scheduler and cron endpoint, API, dashboard (auth, CSRF, escaping), rate limiting, log redaction.

## Deployment

### Render (free plan, no database)
`render.yaml` is a Blueprint for a **free** Docker web service: Gist storage plus cron-triggered syncs.

1. Create the secret Gist and the `gist`-scope token (see [Storage](#storage-no-database)).
2. Push the repo to GitHub or GitLab.
3. In Render, choose **New → Blueprint** and select the repo. Fill in `GIST_ID`, `GITHUB_TOKEN`, `TELEGRAM_*`, `OPENAI_API_KEY`, and so on.
4. After the deploy, open the service's **Environment** tab and copy `SYNC_TOKEN` (and `API_KEY` for the dashboard login).
5. On cron-job.org, schedule `https://<service>.onrender.com/cron/sync?token=<SYNC_TOKEN>` every 3 hours.
6. Check `https://<service>.onrender.com/api/health`. It should show `"storage":"ok","storage_backend":"gist"`.
7. Create the profile: open the dashboard → Profile → save (or upload a CV).

Free-plan behaviour:
- The instance sleeps after 15 minutes without traffic, and each cron hit wakes it.
- While it sleeps, Telegram **commands** (`/jobs`…) get no reply. Alerts and the digest still arrive, because they are sent during syncs.
- The first request after sleeping takes about a minute.
- For instant bot replies, use a paid instance or a VPS.

### Docker (VPS)
```bash
curl -fsSL https://get.docker.com | sudo sh
git clone <your-repo> ai-job-hunter && cd ai-job-hunter
cp .env.example .env && nano .env        # API_KEY, OPENAI_API_KEY, TELEGRAM_* ...
docker compose up -d --build             # JSON files persist in the `appdata` volume
docker compose exec app python -m scripts.seed_profile
curl -s localhost:8000/api/health
# Optional local model:
docker compose --profile ollama up -d && docker compose exec ollama ollama pull llama3.1
```

- **TLS / reverse proxy.** The app listens on `127.0.0.1:8000` only. For example, with Caddy:
  ```
  jobs.example.com {
      reverse_proxy 127.0.0.1:8000
  }
  ```
- **Automatic restart.** `restart: unless-stopped`, plus a container healthcheck on `/api/health`.
- **Logs.** Structured JSON on stdout, rotated by Docker. Secrets and URL tokens are redacted.
- **Monitoring.** Point an uptime monitor at `/api/health`. Failures also arrive in Telegram.
- **Backups.**
  - Copy the JSON files, e.g. `docker compose cp app:/app/storage/data ./backup-YYYY-MM-DD` from a daily cron.
  - Or use `STORAGE_BACKEND=gist`, which keeps the history in the Gist.

### Without Docker (systemd)
```bash
sudo useradd --system --home /opt/ai-job-hunter jobhunter
sudo git clone <your-repo> /opt/ai-job-hunter && cd /opt/ai-job-hunter
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12 && uv sync --frozen --no-dev
cp .env.example .env && nano .env
sudo chown -R jobhunter: /opt/ai-job-hunter
sudo cp scripts/ai-job-hunter.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now ai-job-hunter
journalctl -u ai-job-hunter -f
```

Run **exactly one** instance. The JSON store lives in that process's memory, so multiple instances would overwrite each other's files.

## Security notes

- **Secrets:** environment variables only. `.env` is git-ignored and Docker-ignored. Logs redact keys, tokens and passwords, including Telegram, GitHub and OpenAI tokens and `?token=` query parameters.
- **Cron URL:** disabled unless `SYNC_TOKEN` is set. The token is compared in constant time, and the endpoint can only trigger a sync, not read data.
- **API auth:** a constant-time `X-API-Key` check. If `API_KEY` is unset, the server refuses all requests rather than allowing them. Rate limiting is per IP (`RATE_LIMIT`).
- **Validation:** all input is validated with Pydantic and FastAPI. There is no SQL anywhere, so there's no injection surface.
- **Uploads:** PDF only, checked by magic bytes and size before parsing. Files get random server-side names, and client filenames are never used as paths.
- **SSRF:** job URLs must be public http(s). Private, loopback, link-local and metadata hosts are rejected. Before the server fetches a URL (RSS, dead-link check), DNS is resolved and every address must be public.
- **Output escaping:** job text is untrusted. Jinja2 autoescapes it, Telegram messages escape it, apply buttons are added only for safe URLs, and the dashboard sends CSP, X-Frame-Options and nosniff headers.
- **Mailbox:** opened read-only.
- **OpenAI errors:** messages are sanitised, because some 401 bodies echo part of the key.

## Project layout

```
app/
  main.py              app factory, lifespan (scheduler + bot start/stop), middleware
  config.py            Settings (all env vars)
  container.py         composition root: JSON store, HTTP, providers, collectors, services, Telegram
  logging.py           structlog + redaction
  api/                 routers (jobs, matches, applications, profile, sources, health, cron), dashboard (html.py),
                       deps (auth, pagination), ratelimit
  collectors/          base.py, registry.py, remotive, remoteok, arbeitnow, adzuna, careers, bdjobstoday,
                       email_alerts (+ linkedin, bdjobs parsers), facebook (unavailable)
  ai/                  providers/ (openai, ollama), job_matcher, cv_parser, cover_letter, prompts, skills
  bots/                telegram_bot.py, formatters.py
  scheduler/           setup.py (jobs/triggers), tasks.py (task bodies + scheduler entry points)
  database/            models.py (dataclasses), store.py (JSON store), backends.py (local / gist), repositories/
  services/            collection, matching, notification, profile, document, application, settings, stats
  schemas/             Pydantic DTOs
  utils/               http (retry/rate limit), urls (canonicalize/SSRF), normalize, hashing, text
storage/data/          JSON files (local backend; git-ignored)
templates/             dashboard
scripts/               seed_profile, run_collect_once, docker-entrypoint.sh, systemd unit
tests/                 pytest suite
```

## Known limitations

- **Email parsers.** LinkedIn and BDJobs parsing uses heuristics on alert-email HTML and has not been run against your real emails yet. Alerts carry no job description, so those jobs are scored mostly on title.
- **Adzuna descriptions** are snippets, not the full posting.
- **Rules-only scoring** (no AI provider) is a reasonable ranking but coarse; some borderline roles score high. An LLM provider gives much better results.
- **Single process.** The store and rate limiter live in memory, so run one instance.
- **Scale.** The JSON store is built for a personal job hunter: thousands of jobs, not millions.
- **Gist storage** writes at most once a minute between syncs. A crash in that window can lose the last few minutes of dashboard edits; syncs always write at the end.
- **No OCR.** Scanned PDF CVs can't be parsed.
