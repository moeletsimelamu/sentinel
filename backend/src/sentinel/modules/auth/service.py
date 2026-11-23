"""Authentication: login with lockout, refresh-token rotation with reuse detection, logout."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from sentinel.core.config import Settings
from sentinel.core.errors import UnauthorizedError, ValidationFailedError
from sentinel.core.ids import uuid7
from sentinel.core.logging import get_logger
from sentinel.core.permissions import Permission, Role
from sentinel.core.security import (
    create_access_token,
    hash_password,
    hash_token,
    new_refresh_token,
    verify_password,
)
from sentinel.modules.audit import service as audit
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.auth import repository as repo
from sentinel.modules.auth.models import RefreshToken, User
from sentinel.modules.auth.schemas import Principal

log = get_logger(__name__)

INVALID_CREDENTIALS = "Invalid email or password"
INVALID_SESSION = "Session expired or invalid. Please sign in again."


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    principal: Principal


def to_principal(user: User) -> Principal:
    known = {p.value for p in Permission}
    return Principal(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=Role(user.role.name),
        permissions=frozenset(Permission(p) for p in user.permissions if p in known),
    )


def validate_password_policy(settings: Settings, password: str, email: str) -> None:
    problems: list[str] = []
    if len(password) < settings.password_min_length:
        problems.append(f"must be at least {settings.password_min_length} characters")
    local_part = email.split("@", 1)[0]
    if len(local_part) >= 4 and local_part.lower() in password.lower():
        problems.append("must not contain your email name")
    if password.strip() != password:
        problems.append("must not start or end with whitespace")
    if problems:
        raise ValidationFailedError(
            "Password does not meet the policy",
            details=[{"loc": ["password"], "message": p} for p in problems],
        )


class AuthService:
    def __init__(self, db: Session, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    # ----------------------------------------------------------------- login
    def login(self, email: str, password: str, ctx: RequestContext) -> IssuedTokens:
        now = datetime.now(UTC)
        user = repo.get_user_by_email(self.db, email, for_update=True)

        if user is None:
            verify_password(password, None)  # equalise timing
            audit.record(
                self.db,
                actor=Actor(None, None),
                ctx=ctx,
                action="auth.login_failed",
                entity_type="user",
                after={"reason": "unknown_email"},
            )
            self.db.commit()
            raise UnauthorizedError(INVALID_CREDENTIALS)

        actor = Actor(user.id, user.role.name)

        if user.locked_until is not None and user.locked_until > now:
            verify_password(password, None)
            audit.record(
                self.db,
                actor=actor,
                ctx=ctx,
                action="auth.login_blocked",
                entity_type="user",
                entity_id=user.id,
                after={"reason": "locked"},
            )
            self.db.commit()
            raise UnauthorizedError(INVALID_CREDENTIALS)

        if not verify_password(password, user.password_hash) or not user.is_active:
            reason = "inactive" if user.is_active is False else "bad_password"
            if reason == "bad_password":
                user.failed_login_count += 1
                if user.failed_login_count >= self.settings.max_failed_logins:
                    user.locked_until = now + timedelta(minutes=self.settings.lockout_minutes)
                    user.failed_login_count = 0
                    audit.record(
                        self.db,
                        actor=actor,
                        ctx=ctx,
                        action="auth.account_locked",
                        entity_type="user",
                        entity_id=user.id,
                        after={"locked_until": user.locked_until},
                    )
            audit.record(
                self.db,
                actor=actor,
                ctx=ctx,
                action="auth.login_failed",
                entity_type="user",
                entity_id=user.id,
                after={"reason": reason},
            )
            self.db.commit()
            raise UnauthorizedError(INVALID_CREDENTIALS)

        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        tokens = self._issue(user, family_id=uuid7(), now=now)
        audit.record(
            self.db,
            actor=actor,
            ctx=ctx,
            action="auth.login_succeeded",
            entity_type="user",
            entity_id=user.id,
        )
        self.db.commit()
        log.info("login_succeeded", user_id=str(user.id))
        return tokens

    # --------------------------------------------------------------- refresh
    def refresh(self, raw_token: str | None, ctx: RequestContext) -> IssuedTokens:
        if not raw_token:
            raise UnauthorizedError(INVALID_SESSION)
        now = datetime.now(UTC)
        token = repo.get_refresh_token_by_hash(self.db, hash_token(raw_token))
        if token is None:
            raise UnauthorizedError(INVALID_SESSION)

        if token.revoked_at is not None:
            if token.replaced_by_id is not None:
                # A rotated token was presented again: assume theft, end the whole session.
                repo.revoke_family(self.db, token.family_id, now)
                audit.record(
                    self.db,
                    actor=Actor(token.user_id, None),
                    ctx=ctx,
                    action="auth.refresh_reuse_detected",
                    entity_type="user",
                    entity_id=token.user_id,
                    after={"family_id": token.family_id},
                )
                self.db.commit()
                log.warning("refresh_token_reuse_detected", user_id=str(token.user_id))
            raise UnauthorizedError(INVALID_SESSION)

        if token.expires_at <= now:
            raise UnauthorizedError(INVALID_SESSION)

        user = repo.get_user(self.db, token.user_id)
        if user is None or not user.is_active:
            repo.revoke_family(self.db, token.family_id, now)
            self.db.commit()
            raise UnauthorizedError(INVALID_SESSION)

        tokens = self._issue(user, family_id=token.family_id, now=now, replaces=token)
        self.db.commit()
        return tokens

    # ---------------------------------------------------------------- logout
    def logout(self, raw_token: str | None, ctx: RequestContext) -> None:
        if not raw_token:
            return
        token = repo.get_refresh_token_by_hash(self.db, hash_token(raw_token))
        if token is None:
            return
        repo.revoke_family(self.db, token.family_id, datetime.now(UTC))
        audit.record(
            self.db,
            actor=Actor(token.user_id, None),
            ctx=ctx,
            action="auth.logout",
            entity_type="user",
            entity_id=token.user_id,
        )
        self.db.commit()

    # ------------------------------------------------------- change password
    def change_password(
        self, principal: Principal, current: str, new: str, ctx: RequestContext
    ) -> None:
        user = repo.get_user(self.db, principal.id, for_update=True)
        if user is None or not verify_password(current, user.password_hash):
            raise UnauthorizedError("Current password is incorrect")
        validate_password_policy(self.settings, new, user.email)
        if verify_password(new, user.password_hash):
            raise ValidationFailedError("New password must differ from the current password")
        now = datetime.now(UTC)
        user.password_hash = hash_password(new)
        user.password_changed_at = now
        user.version += 1
        repo.revoke_all_for_user(self.db, user.id, now)
        audit.record(
            self.db,
            actor=Actor(user.id, user.role.name),
            ctx=ctx,
            action="auth.password_changed",
            entity_type="user",
            entity_id=user.id,
        )
        self.db.commit()

    # -------------------------------------------------------------- internals
    def load_principal(self, user_id: uuid.UUID) -> Principal | None:
        user = repo.get_user(self.db, user_id)
        if user is None or not user.is_active:
            return None
        return to_principal(user)

    def _issue(
        self,
        user: User,
        *,
        family_id: uuid.UUID,
        now: datetime,
        replaces: RefreshToken | None = None,
    ) -> IssuedTokens:
        access, access_exp = create_access_token(self.settings, user.id, user.role.name, now)
        raw_refresh = new_refresh_token()
        refresh_exp = now + timedelta(days=self.settings.refresh_token_ttl_days)
        new_token = RefreshToken(
            id=uuid7(),
            user_id=user.id,
            token_hash=hash_token(raw_refresh),
            family_id=family_id,
            issued_at=now,
            expires_at=refresh_exp,
        )
        self.db.add(new_token)
        if replaces is not None:
            self.db.flush()
            replaces.revoked_at = now
            replaces.replaced_by_id = new_token.id
        return IssuedTokens(access, access_exp, raw_refresh, refresh_exp, to_principal(user))
