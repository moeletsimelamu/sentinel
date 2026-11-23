"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi.errors import RateLimitExceeded

from sentinel import __version__
from sentinel.api.router import api_router
from sentinel.core.config import get_settings
from sentinel.core.errors import register_exception_handlers
from sentinel.core.logging import configure_logging
from sentinel.core.middleware import RequestContextMiddleware
from sentinel.core.rate_limit import limiter, rate_limit_exceeded_handler


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    docs = settings.env != "production"
    app = FastAPI(
        title="Sentinel API",
        version=__version__,
        description=(
            "Fraud detection and risk analytics — educational simulation using synthetic data. "
            "Risk scores are investigative indicators, not proof of fraud."
        ),
        docs_url="/api/docs" if docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs else None,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    register_exception_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    # Added last → outermost: every response, including CORS preflights, gets a request id.
    app.add_middleware(RequestContextMiddleware)

    app.include_router(api_router)
    return app


app = create_app()
