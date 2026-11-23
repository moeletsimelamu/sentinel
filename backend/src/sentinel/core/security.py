"""Password hashing, JWT access tokens, opaque refresh tokens and identifier hashing."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from sentinel.core.config import Settings

_password_hash = PasswordHash((Argon2Hasher(),))

# Used to equalise timing when the email does not exist (prevents user enumeration).
_DUMMY_HASH = _password_hash.hash("timing-equaliser-not-a-real-password")

ACCESS_TOKEN_TYPE = "access"  # noqa: S105 — token type label, not a secret
JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        _password_hash.verify(password, _DUMMY_HASH)
        return False
    return _password_hash.verify(password, password_hash)


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    role: str
    token_id: str
    expires_at: datetime


def create_access_token(
    settings: Settings, user_id: uuid.UUID, role: str, now: datetime | None = None
) -> tuple[str, datetime]:
    issued = now or datetime.now(UTC)
    expires = issued + timedelta(minutes=settings.access_token_ttl_minutes)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "type": ACCESS_TOKEN_TYPE,
        "jti": secrets.token_hex(16),
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)
    return token, expires


def decode_access_token(settings: Settings, token: str) -> AccessTokenClaims:
    """Raises ``jwt.PyJWTError`` for any invalid, expired or mis-typed token."""
    payload = jwt.decode(
        token,
        settings.jwt_secret.get_secret_value(),
        algorithms=[JWT_ALGORITHM],
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        options={"require": ["exp", "iat", "sub", "jti", "type"]},
    )
    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise jwt.InvalidTokenError("wrong token type")
    try:
        user_id = uuid.UUID(payload["sub"])
    except ValueError as exc:
        raise jwt.InvalidTokenError("invalid subject") from exc
    return AccessTokenClaims(
        user_id=user_id,
        role=str(payload.get("role", "")),
        token_id=str(payload["jti"]),
        expires_at=datetime.fromtimestamp(payload["exp"], UTC),
    )


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """Refresh tokens are high-entropy, so a fast hash is appropriate (no need for Argon2)."""
    return hashlib.sha256(token.encode()).hexdigest()


def hash_identifier(settings: Settings, value: str) -> str:
    """Keyed hash for identifiers that only need equality comparisons (IP, device id)."""
    key = settings.hash_salt.get_secret_value().encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()
