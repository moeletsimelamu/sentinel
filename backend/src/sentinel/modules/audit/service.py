"""Audit trail writer and reader.

``record`` adds the audit row to the caller's session so it commits (or rolls back)
atomically with the business change it describes.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.core.logging import redact
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.audit.models import AuditLog


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted((_jsonable(v) for v in value), key=str)
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def diff(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return only the fields that changed, as (before, after)."""
    keys = sorted(set(before) | set(after))
    changed = [k for k in keys if before.get(k) != after.get(k)]
    return ({k: before.get(k) for k in changed}, {k: after.get(k) for k in changed})


def record(
    db: Session,
    *,
    actor: Actor,
    ctx: RequestContext,
    action: str,
    entity_type: str,
    entity_id: str | uuid.UUID | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor.id,
        actor_role=actor.role,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        before=redact(_jsonable(before)) if before is not None else None,
        after=redact(_jsonable(after)) if after is not None else None,
        request_id=ctx.request_id,
        ip_hash=ctx.ip_hash,
    )
    db.add(entry)
    return entry


def list_entries(
    db: Session,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    before_id: int | None = None,
    limit: int = 50,
) -> Sequence[AuditLog]:
    """Newest first, keyset-paginated on the monotonically increasing ``id``."""
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    if entity_type:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    return db.scalars(stmt).all()
