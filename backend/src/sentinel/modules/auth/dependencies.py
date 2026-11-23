"""Authentication and authorisation dependencies.

Every protected route declares ``Depends(RequirePermission(Permission.X))``. The
permission-matrix test discovers these dependencies on every route, so a route that
forgets one fails CI unless it is on the explicit public / authenticated-only allowlist.
"""

from __future__ import annotations

import jwt
import structlog
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from sentinel.core.config import Settings, get_settings
from sentinel.core.db import get_db
from sentinel.core.errors import ForbiddenError, UnauthorizedError
from sentinel.core.permissions import Permission
from sentinel.core.security import decode_access_token
from sentinel.modules.auth.schemas import Principal
from sentinel.modules.auth.service import AuthService

_bearer = HTTPBearer(auto_error=False)


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("Authentication required")
    try:
        claims = decode_access_token(settings, credentials.credentials)
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("Invalid or expired access token") from exc
    # The user is re-loaded on every request so deactivation and role changes apply
    # immediately rather than when the access token expires.
    principal = AuthService(db, settings).load_principal(claims.user_id)
    if principal is None:
        raise UnauthorizedError("Invalid or expired access token")
    structlog.contextvars.bind_contextvars(user_id=str(principal.id), role=principal.role.value)
    return principal


class RequirePermission:
    """Dependency that authenticates the caller and checks one permission."""

    def __init__(self, permission: Permission) -> None:
        self.permission = permission

    def __call__(self, principal: Principal = Depends(get_current_principal)) -> Principal:
        if self.permission not in principal.permissions:
            raise ForbiddenError(
                "You do not have permission to perform this action",
                details={"required_permission": self.permission.value},
            )
        return principal

    def __repr__(self) -> str:
        return f"RequirePermission({self.permission.value})"
