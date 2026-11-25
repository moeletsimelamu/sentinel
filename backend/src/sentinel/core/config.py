"""Application settings, loaded from environment variables (prefix ``SENTINEL_``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]

# Values that are acceptable only on a developer machine or in CI.
_INSECURE_DEFAULT_SECRET = "dev-only-insecure-secret-change-me-0000000000"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SENTINEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = "development"
    log_level: str = "INFO"
    log_json: bool = True

    database_url: str = "postgresql+psycopg://sentinel:sentinel@localhost:5438/sentinel"

    # --- authentication -------------------------------------------------
    jwt_secret: SecretStr = SecretStr(_INSECURE_DEFAULT_SECRET)
    jwt_issuer: str = "sentinel-api"
    jwt_audience: str = "sentinel-ui"
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_ttl_days: int = Field(default=7, ge=1, le=30)
    refresh_cookie_name: str = "sentinel_refresh"
    cookie_secure: bool = True
    max_failed_logins: int = Field(default=5, ge=1)
    lockout_minutes: int = Field(default=15, ge=1)
    password_min_length: int = Field(default=12, ge=8)

    # --- hashing of identifiers that only need equality (IPs, device ids)
    hash_salt: SecretStr = SecretStr("dev-only-salt")

    # --- HTTP ------------------------------------------------------------
    cors_origins: list[str] = ["http://localhost:5173"]
    login_rate_limit: str = "10/minute"
    ingestion_rate_limit: str = "30/minute"
    rate_limit_enabled: bool = True

    # --- ingestion ---------------------------------------------------------
    import_max_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)
    import_max_rows: int = Field(default=250_000, ge=1)
    api_ingest_max_records: int = Field(default=1000, ge=1, le=10_000)

    # --- model registry ------------------------------------------------------
    models_dir: Path = Path("models")  # root of model artefact folders (read-only mount)

    # --- background jobs ---------------------------------------------------
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    job_retry_base_seconds: int = Field(default=10, ge=1)
    job_lock_timeout_minutes: int = Field(default=15, ge=1)
    worker_poll_seconds: float = Field(default=2.0, gt=0)

    @model_validator(mode="after")
    def _refuse_insecure_production(self) -> Settings:
        if self.env == "production":
            secret = self.jwt_secret.get_secret_value()
            if secret == _INSECURE_DEFAULT_SECRET or len(secret) < 32:
                raise ValueError("SENTINEL_JWT_SECRET must be set to a strong value in production")
            if self.hash_salt.get_secret_value() == "dev-only-salt":
                raise ValueError("SENTINEL_HASH_SALT must be set in production")
            if not self.cookie_secure:
                raise ValueError("SENTINEL_COOKIE_SECURE must be true in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
