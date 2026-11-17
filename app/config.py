"""Application configuration loaded from environment variables / .env."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class CareerSource(BaseModel):
    """A company career board reachable through an official public API or feed."""

    kind: Literal["greenhouse", "lever", "ashby", "rss"]
    # Board token / company slug for ATS kinds; feed URL for rss.
    identifier: str
    company: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_env: Literal["development", "production", "test"] = "development"
    app_name: str = "AI Job Hunter"
    api_key: str = ""
    log_level: str = "INFO"
    log_json: bool = True
    timezone: str = "Asia/Dhaka"
    run_scheduler: bool = True
    run_bot: bool = True
    storage_dir: Path = BASE_DIR / "storage"
    rate_limit: str = "60/minute"

    # --- Storage (JSON files, no database) ---
    storage_backend: Literal["local", "gist"] = "local"
    data_path: Path | None = None  # where JSON files go (local backend); default storage/data
    gist_id: str = ""
    github_token: str = ""
    storage_flush_seconds: float = 60.0  # min seconds between remote (gist) writes; forced flushes ignore it
    max_jobs: int = 3000  # oldest jobs beyond this are pruned nightly
    description_store_limit: int = 8000  # characters of job description kept

    # --- Sync trigger (cron URL) ---
    sync_token: str = ""  # enables GET /cron/sync?token=...; empty disables it

    # --- AI ---
    ai_provider: Literal["openai", "ollama", "none"] = "openai"
    ai_fallback_provider: Literal["openai", "ollama", "none"] = "none"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    ai_timeout: float = 60.0
    prefilter_min_score: int = 35
    alert_threshold: int = 80
    max_ai_matches_per_run: int = 100

    # --- Telegram ---
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- Adzuna ---
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    adzuna_countries: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["gb", "us", "de", "sg"])
    adzuna_queries: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["laravel developer", "php developer", "python backend developer"]
    )

    # --- Email alerts (LinkedIn / BDJobs) ---
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_lookback_days: int = 3
    linkedin_alert_senders: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com"]
    )
    bdjobs_alert_senders: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["bdjobs.com"])
    # BDJobsToday category ids: 3 = IT/Telecom/Creative Design, 5 = Engineer & Architects
    bdjobstoday_categories: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [3, 5])

    # --- Collectors ---
    career_sources: Annotated[list[CareerSource], NoDecode] = Field(default_factory=list)
    search_keywords: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["laravel", "php", "python", "fastapi", "full stack", "backend"]
    )
    collector_concurrency: int = 4
    collector_timeout: float = 300.0
    relevance_filter: bool = True
    http_timeout: float = 20.0
    http_max_retries: int = 3
    http_rate_per_host: float = 1.0  # requests per second per host
    job_max_age_days: int = 45
    collect_interval_hours: int = 3
    digest_hour: int = 9

    @field_validator("telegram_allowed_chat_ids", "bdjobstoday_categories", mode="before")
    @classmethod
    def _parse_int_list(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [int(x) for x in v.replace(";", ",").split(",") if x.strip()]
        return v

    @field_validator(
        "adzuna_countries",
        "adzuna_queries",
        "search_keywords",
        "linkedin_alert_senders",
        "bdjobs_alert_senders",
        mode="before",
    )
    @classmethod
    def _parse_str_list(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                return json.loads(v)
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("career_sources", mode="before")
    @classmethod
    def _parse_career_sources(cls, v: Any) -> Any:
        if isinstance(v, str):
            return json.loads(v) if v.strip() else []
        return v

    # --- Derived ---
    @property
    def data_dir(self) -> Path:
        return self.data_path or (self.storage_dir / "data")

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_allowed_chat_ids)


@lru_cache
def get_settings() -> Settings:
    return Settings()
