"""Structured logging with secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

SENSITIVE_KEYS = re.compile(r"(pass(word)?|secret|token|api[_-]?key|authorization|cookie|app_key)", re.I)
# Telegram bot tokens / OpenAI keys appearing inside free text (e.g. URLs in exception messages).
SENSITIVE_VALUES = re.compile(r"(\d{6,}:[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,})")
# Secrets passed in URLs, e.g. /cron/sync?token=... in access logs.
SENSITIVE_QUERY = re.compile(r"((?:token|key|api_key|app_key)=)[^&\s\"']+", re.I)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return SENSITIVE_QUERY.sub(r"\1***", SENSITIVE_VALUES.sub("***", value))
    return value


def redact_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict):
        if SENSITIVE_KEYS.search(key):
            event_dict[key] = "***"
        else:
            event_dict[key] = _redact_value(event_dict[key])
    return event_dict


class _RedactingFilter(logging.Filter):
    """Redact secrets from stdlib loggers (httpx logs full URLs incl. telegram bot token)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_value(record.msg)
        if record.args:
            record.args = (
                tuple(_redact_value(a) for a in record.args) if isinstance(record.args, tuple) else record.args
            )
        return True


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_processor,
    ]
    renderer: Any = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(_RedactingFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "apscheduler.executors.default", "telegram.ext"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # uvicorn's loggers have their own handlers (propagate=False): filter at the logger itself so
    # e.g. "GET /cron/sync?token=..." never reaches the access log unredacted.
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _RedactingFilter) for f in logger.filters):
            logger.addFilter(_RedactingFilter())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
