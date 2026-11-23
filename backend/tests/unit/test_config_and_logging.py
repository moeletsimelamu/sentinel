from __future__ import annotations

import pytest
from pydantic import ValidationError

from sentinel.core.config import Settings
from sentinel.core.logging import REDACTED, redact, redact_processor


def test_production_refuses_default_secret() -> None:
    with pytest.raises(ValidationError, match="SENTINEL_JWT_SECRET"):
        Settings(env="production")


def test_production_refuses_short_secret() -> None:
    with pytest.raises(ValidationError, match="SENTINEL_JWT_SECRET"):
        Settings(env="production", jwt_secret="short", hash_salt="s" * 32)


def test_production_requires_secure_cookies() -> None:
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        Settings(env="production", jwt_secret="p" * 48, hash_salt="s" * 32, cookie_secure=False)


def test_production_accepts_strong_config() -> None:
    s = Settings(env="production", jwt_secret="p" * 48, hash_salt="s" * 32, cookie_secure=True)
    assert s.env == "production"


def test_redact_nested_structures() -> None:
    data = {
        "email": "a@b.c",
        "password": "hunter2",
        "nested": {"refresh_token": "abc", "items": [{"Authorization": "Bearer x"}]},
        "company": "kept",
        "pan": "4111",
    }
    out = redact(data)
    assert out["password"] == REDACTED
    assert out["nested"]["refresh_token"] == REDACTED
    assert out["nested"]["items"][0]["Authorization"] == REDACTED
    assert out["pan"] == REDACTED
    assert out["company"] == "kept"  # 'pan' substring must not over-match
    assert out["email"] == "a@b.c"


def test_redact_processor_masks_top_level_keys() -> None:
    event = {"event": "login", "new_password": "x", "user_id": "u1"}
    out = redact_processor(None, "info", event)
    assert out["new_password"] == REDACTED
    assert out["user_id"] == "u1"
