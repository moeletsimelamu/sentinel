"""Structured logging with sensitive-value redaction."""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

REDACTED = "[REDACTED]"

# Keys whose values must never reach logs. Matched case-insensitively as substrings
# so variants such as ``new_password`` or ``x-refresh-token`` are also covered.
SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "jwt",
    "api_key",
    "apikey",
    "card_number",
    "pan",
    "cvv",
)


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered == "pan":
        return True
    return any(part in lowered for part in SENSITIVE_KEY_PARTS if part != "pan")


def redact(value: Any) -> Any:
    """Recursively redact sensitive keys in mappings and sequences."""
    if isinstance(value, Mapping):
        return {
            k: (REDACTED if isinstance(k, str) and _is_sensitive(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return type(value)(redact(v) for v in value)
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict.keys()):
        if _is_sensitive(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact(event_dict[key])
    return event_dict


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact_processor,
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=True,
    )
    # Route stdlib loggers (uvicorn, sqlalchemy, alembic) through the same level.
    logging.basicConfig(level=level.upper(), stream=sys.stdout, format="%(message)s")


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
