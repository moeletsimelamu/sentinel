from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from sentinel.core.db import get_db
from sentinel.core.permissions import Permission
from sentinel.modules.audit import service
from sentinel.modules.audit.schemas import AuditEntryOut, AuditPage
from sentinel.modules.auth.dependencies import RequirePermission

router = APIRouter(prefix="/audit-log", tags=["audit"])


@router.get(
    "",
    response_model=AuditPage,
    dependencies=[Depends(RequirePermission(Permission.AUDIT_READ))],
)
def list_audit_entries(
    entity_type: str | None = Query(default=None, max_length=64),
    entity_id: str | None = Query(default=None, max_length=64),
    actor_id: uuid.UUID | None = None,
    action: str | None = Query(default=None, max_length=64),
    cursor: int | None = Query(default=None, ge=1, description="Return entries older than this id"),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> AuditPage:
    rows = service.list_entries(
        db,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        action=action,
        before_id=cursor,
        limit=limit,
    )
    items = [AuditEntryOut.model_validate(r) for r in rows]
    next_cursor = items[-1].id if len(items) == limit else None
    return AuditPage(items=items, next_cursor=next_cursor)
