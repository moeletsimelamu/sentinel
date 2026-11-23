from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import SecretStr

from sentinel.core.config import Settings
from sentinel.core.ids import uuid7
from sentinel.core.security import (
    create_access_token,
    decode_access_token,
    hash_identifier,
    hash_password,
    hash_token,
    verify_password,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(env="test", jwt_secret="unit-test-secret-" + "x" * 32)


def test_password_hash_roundtrip() -> None:
    hashed = hash_password("a-long-test-passphrase")
    assert hashed.startswith("$argon2id$")
    assert verify_password("a-long-test-passphrase", hashed)
    assert not verify_password("wrong-passphrase", hashed)


def test_verify_against_missing_hash_is_false() -> None:
    assert verify_password("anything", None) is False


def test_access_token_roundtrip(settings: Settings) -> None:
    uid = uuid.uuid4()
    token, expires = create_access_token(settings, uid, "analyst")
    claims = decode_access_token(settings, token)
    assert claims.user_id == uid
    assert claims.role == "analyst"
    assert claims.expires_at == expires.replace(microsecond=0)


def test_expired_token_rejected(settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(hours=1)
    token, _ = create_access_token(settings, uuid.uuid4(), "analyst", now=past)
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(settings, token)


def test_token_signed_with_other_key_rejected(settings: Settings) -> None:
    other = Settings(env="test", jwt_secret="another-secret-" + "y" * 32)
    token, _ = create_access_token(other, uuid.uuid4(), "admin")
    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(settings, token)


def test_wrong_audience_rejected(settings: Settings) -> None:
    other = settings.model_copy(update={"jwt_audience": "someone-else"})
    token, _ = create_access_token(other, uuid.uuid4(), "admin")
    with pytest.raises(jwt.InvalidAudienceError):
        decode_access_token(settings, token)


def test_non_access_token_type_rejected(settings: Settings) -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid.uuid4()),
        "type": "refresh",
        "jti": "x",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm="HS256")
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(settings, token)


def test_alg_none_rejected(settings: Settings) -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid.uuid4()),
        "type": "access",
        "jti": "x",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    token = jwt.encode(payload, key=None, algorithm="none")
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(settings, token)


def test_token_and_identifier_hashing_are_deterministic(settings: Settings) -> None:
    assert hash_token("abc") == hash_token("abc") != hash_token("abd")
    assert len(hash_identifier(settings, "10.0.0.1")) == 64
    other = settings.model_copy(update={"hash_salt": SecretStr("different-salt")})
    assert hash_identifier(settings, "10.0.0.1") != hash_identifier(other, "10.0.0.1")


def test_uuid7_version_and_ordering() -> None:
    ids = [uuid7() for _ in range(200)]
    assert all(u.version == 7 for u in ids)
    assert all(u.variant == uuid.RFC_4122 for u in ids)
    # Time-ordered at millisecond granularity.
    assert [u.int >> 80 for u in ids] == sorted(u.int >> 80 for u in ids)
