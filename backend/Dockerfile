# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.13.9 AS uv

# ---------------------------------------------------------------- build stage
FROM python:3.12-slim AS builder
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build

# Dependencies first for layer caching.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# -------------------------------------------------------------- runtime stage
FROM python:3.12-slim AS runtime
RUN groupadd --system sentinel && useradd --system --gid sentinel --no-create-home sentinel

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini ./
COPY alembic ./alembic

USER sentinel
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "sentinel.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
