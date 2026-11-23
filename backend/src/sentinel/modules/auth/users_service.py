"""User administration. Every change is versioned (optimistic locking) and audited."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sentinel.core.config import Settings
from sentinel.core.errors import ConflictError, NotFoundError, ValidationFailedError
from sentinel.core.permissions import GRANTABLE_TO, Permission, Role
from sentinel.core.security import hash_password
from sentinel.modules.audit import service as audit
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.auth import repository as repo
from sentinel.modules.auth.models import User, UserPermissionGrant
from sentinel.modules.auth.schemas import Principal, UserCreate, UserOut, UserUpdate
from sentinel.modules.auth.service import validate_password_policy


def to_out(user: User) -> UserOut:
    now = datetime.now(UTC)
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=Role(user.role.name),
        is_active=user.is_active,
        is_locked=user.locked_until is not None and user.locked_until > now,
        grants=sorted(Permission(g.permission) for g in user.grants),
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        version=user.version,
    )


def _snapshot(user: User) -> dict[str, Any]:
    """Audit-safe view of a user — never includes the password hash."""
    return {
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role.name,
        "is_active": user.is_active,
        "grants": sorted(g.permission for g in user.grants),
    }


class UserAdminService:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    def list(
        self, *, role: Role | None, is_active: bool | None, limit: int, offset: int
    ) -> tuple[list[UserOut], int]:
        role_id = None
        if role is not None:
            role_row = repo.get_role_by_name(self.db, role.value)
            role_id = role_row.id if role_row else -1
        rows, total = repo.list_users(
            self.db, role_id=role_id, is_active=is_active, limit=limit, offset=offset
        )
        return [to_out(u) for u in rows], total

    def get(self, user_id: uuid.UUID) -> UserOut:
        return to_out(self._get(user_id))

    def create(self, data: UserCreate, by: Principal, ctx: RequestContext) -> UserOut:
        validate_password_policy(self.settings, data.password, data.email)
        if repo.get_user_by_email(self.db, data.email) is not None:
            raise ConflictError("A user with this email already exists", code="email_taken")
        role = repo.get_role_by_name(self.db, data.role.value)
        if role is None:  # pragma: no cover — roles are seeded by migration
            raise ValidationFailedError("Unknown role")
        user = User(
            email=data.email,
            full_name=data.full_name,
            password_hash=hash_password(data.password),
            role=role,
            role_id=role.id,
        )
        self.db.add(user)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            raise ConflictError(
                "A user with this email already exists", code="email_taken"
            ) from exc
        audit.record(
            self.db,
            actor=Actor(by.id, by.role.value),
            ctx=ctx,
            action="user.created",
            entity_type="user",
            entity_id=user.id,
            after=_snapshot(user),
        )
        self.db.commit()
        return to_out(user)

    def update(
        self, user_id: uuid.UUID, data: UserUpdate, by: Principal, ctx: RequestContext
    ) -> UserOut:
        user = self._get(user_id, for_update=True)
        self._check_version(user, data.version)
        before = _snapshot(user)

        if user.id == by.id and (
            (data.role is not None and data.role.value != user.role.name) or data.is_active is False
        ):
            raise ValidationFailedError(
                "You cannot change your own role or deactivate yourself",
                code="self_modification_forbidden",
            )

        if data.full_name is not None:
            user.full_name = data.full_name
        if data.role is not None and data.role.value != user.role.name:
            role = repo.get_role_by_name(self.db, data.role.value)
            if role is None:  # pragma: no cover
                raise ValidationFailedError("Unknown role")
            user.role = role
            user.role_id = role.id
            # Drop grants the new role is not allowed to hold.
            user.grants = [
                g
                for g in user.grants
                if data.role in GRANTABLE_TO.get(Permission(g.permission), frozenset())
            ]
        if data.is_active is not None and data.is_active != user.is_active:
            user.is_active = data.is_active
            if not data.is_active:
                repo.revoke_all_for_user(self.db, user.id, datetime.now(UTC))

        after = _snapshot(user)
        if after == before:
            return to_out(user)
        user.version += 1
        b, a = audit.diff(before, after)
        audit.record(
            self.db,
            actor=Actor(by.id, by.role.value),
            ctx=ctx,
            action="user.updated",
            entity_type="user",
            entity_id=user.id,
            before=b,
            after=a,
        )
        self.db.commit()
        return to_out(user)

    def grant(
        self,
        user_id: uuid.UUID,
        permission: Permission,
        version: int,
        by: Principal,
        ctx: RequestContext,
    ) -> UserOut:
        user = self._get(user_id, for_update=True)
        self._check_version(user, version)
        allowed_roles = GRANTABLE_TO.get(permission)
        if allowed_roles is None:
            raise ValidationFailedError("This permission cannot be granted individually")
        if Role(user.role.name) not in allowed_roles:
            raise ValidationFailedError(
                f"Permission {permission.value} cannot be granted to role {user.role.name}"
            )
        if any(g.permission == permission.value for g in user.grants):
            return to_out(user)
        before = _snapshot(user)
        user.grants.append(
            UserPermissionGrant(user_id=user.id, permission=permission.value, granted_by_id=by.id)
        )
        return self._commit_grant_change(user, before, by, ctx, "user.permission_granted")

    def revoke(
        self,
        user_id: uuid.UUID,
        permission: Permission,
        version: int,
        by: Principal,
        ctx: RequestContext,
    ) -> UserOut:
        user = self._get(user_id, for_update=True)
        self._check_version(user, version)
        if not any(g.permission == permission.value for g in user.grants):
            raise NotFoundError("The user does not hold this grant")
        before = _snapshot(user)
        user.grants = [g for g in user.grants if g.permission != permission.value]
        return self._commit_grant_change(user, before, by, ctx, "user.permission_revoked")

    # -------------------------------------------------------------- internals
    def _commit_grant_change(
        self, user: User, before: dict[str, Any], by: Principal, ctx: RequestContext, action: str
    ) -> UserOut:
        user.version += 1
        b, a = audit.diff(before, _snapshot(user))
        audit.record(
            self.db,
            actor=Actor(by.id, by.role.value),
            ctx=ctx,
            action=action,
            entity_type="user",
            entity_id=user.id,
            before=b,
            after=a,
        )
        self.db.commit()
        return to_out(user)

    def _get(self, user_id: uuid.UUID, *, for_update: bool = False) -> User:
        user = repo.get_user(self.db, user_id, for_update=for_update)
        if user is None:
            raise NotFoundError("User not found")
        return user

    @staticmethod
    def _check_version(user: User, version: int) -> None:
        if user.version != version:
            raise ConflictError(
                "This user was changed by someone else. Reload and try again.",
                code="stale_version",
                details={"current_version": user.version},
            )
