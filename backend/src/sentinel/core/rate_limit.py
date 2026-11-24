"""Rate limiting (slowapi). In-memory storage: correct for a single API process.

Behind a reverse proxy, the client address must come from a trusted proxy header —
documented in SECURITY.md; uvicorn is started with ``--proxy-headers`` only when a
trusted proxy is configured.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from sentinel.core.config import get_settings
from sentinel.core.errors import error_response

limiter = Limiter(key_func=get_remote_address, enabled=get_settings().rate_limit_enabled)


def login_limit() -> str:
    return get_settings().login_rate_limit


def ingestion_limit() -> str:
    return get_settings().ingestion_rate_limit


async def rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RateLimitExceeded):  # pragma: no cover - registered for this type only
        raise exc
    response = error_response(
        request, 429, "rate_limited", "Too many requests. Please wait and try again."
    )
    response.headers["Retry-After"] = "60"
    return response
