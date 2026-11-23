from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from sentinel.core.permissions import Permission, Role

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def _normalise_email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254 or not _EMAIL_RE.match(value):
        raise ValueError("must be a valid email address")
    return value


Email = Annotated[str, AfterValidator(_normalise_email)]
# Upper bound protects the Argon2 hasher from very large inputs.
Password = Annotated[str, Field(min_length=1, max_length=128)]
FullName = Annotated[str, Field(min_length=1, max_length=120)]


class LoginRequest(BaseModel):
    email: Email
    password: Password


class ChangePasswordRequest(BaseModel):
    current_password: Password
    new_password: Password


class Principal(BaseModel):
    """The authenticated caller, as seen by services and permission checks."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    permissions: frozenset[Permission]


class MeResponse(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    permissions: list[Permission]


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 — OAuth2 token type label
    expires_at: datetime
    user: MeResponse


# ------------------------------------------------------------- user management


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    is_active: bool
    is_locked: bool
    grants: list[Permission]
    last_login_at: datetime | None
    created_at: datetime
    version: int


class UserCreate(BaseModel):
    email: Email
    full_name: FullName
    role: Role
    password: Password


class UserUpdate(BaseModel):
    version: int = Field(ge=1, description="Current version; stale versions are rejected (409)")
    full_name: FullName | None = None
    role: Role | None = None
    is_active: bool | None = None


class GrantRequest(BaseModel):
    version: int = Field(ge=1)
    permission: Permission


class UserPage(BaseModel):
    items: list[UserOut]
    total: int
    limit: int
    offset: int
