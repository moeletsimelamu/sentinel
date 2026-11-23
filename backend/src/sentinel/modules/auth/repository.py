from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from sentinel.modules.auth.models import RefreshToken, Role, User


def get_user(db: Session, user_id: uuid.UUID, *, for_update: bool = False) -> User | None:
    stmt = select(User).where(User.id == user_id)
    if for_update:
        stmt = stmt.with_for_update(of=User)
    return db.scalars(stmt).unique().one_or_none()


def get_user_by_email(db: Session, email: str, *, for_update: bool = False) -> User | None:
    stmt = select(User).where(User.email == email)
    if for_update:
        stmt = stmt.with_for_update(of=User)
    return db.scalars(stmt).unique().one_or_none()


def get_role_by_name(db: Session, name: str) -> Role | None:
    return db.scalars(select(Role).where(Role.name == name)).one_or_none()


def list_users(
    db: Session, *, role_id: int | None, is_active: bool | None, limit: int, offset: int
) -> tuple[Sequence[User], int]:
    stmt = select(User)
    count_stmt = select(func.count()).select_from(User)
    if role_id is not None:
        stmt = stmt.where(User.role_id == role_id)
        count_stmt = count_stmt.where(User.role_id == role_id)
    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
        count_stmt = count_stmt.where(User.is_active == is_active)
    rows = db.scalars(stmt.order_by(User.email).limit(limit).offset(offset)).unique().all()
    total = db.scalar(count_stmt) or 0
    return rows, total


def get_refresh_token_by_hash(db: Session, token_hash: str) -> RefreshToken | None:
    stmt = select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    return db.scalars(stmt).one_or_none()


def revoke_family(db: Session, family_id: uuid.UUID, at: datetime) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=at)
    )


def revoke_all_for_user(db: Session, user_id: uuid.UUID, at: datetime) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=at)
    )
