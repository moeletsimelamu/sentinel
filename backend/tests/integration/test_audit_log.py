from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from sentinel.core.permissions import Role
from sentinel.modules.audit import service as audit
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.audit.models import AuditLog
from tests.conftest import AuthHeaders

pytestmark = pytest.mark.integration


def _entry(db: Session) -> AuditLog:
    entry = audit.record(
        db,
        actor=Actor.system(),
        ctx=RequestContext.offline(),
        action="test.event",
        entity_type="test",
        entity_id="1",
        after={"x": 1},
    )
    db.flush()
    return entry


def test_audit_rows_cannot_be_updated(db: Session) -> None:
    entry = _entry(db)
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(
            text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"), {"id": entry.id}
        )


def test_audit_rows_cannot_be_deleted(db: Session) -> None:
    entry = _entry(db)
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": entry.id})


def test_audit_table_cannot_be_truncated(db: Session) -> None:
    _entry(db)
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text("TRUNCATE audit_log"))


def test_audit_record_redacts_sensitive_values(db: Session) -> None:
    entry = audit.record(
        db,
        actor=Actor.system(),
        ctx=RequestContext.offline(),
        action="test.event",
        entity_type="test",
        after={"password": "secret!", "note": "fine"},
    )
    db.flush()
    db.refresh(entry)
    assert entry.after == {"password": "[REDACTED]", "note": "fine"}


def test_user_admin_actions_are_audited_without_secrets(
    client: TestClient, auth_headers: AuthHeaders, db: Session
) -> None:
    headers = auth_headers(Role.ADMIN)
    resp = client.post(
        "/api/v1/users",
        json={
            "email": "new.analyst@test.local",
            "full_name": "New Analyst",
            "role": "analyst",
            "password": "Plenty-long-pass-77",
        },
        headers=headers,
    )
    assert resp.status_code == 201
    user = resp.json()

    entry = db.scalars(
        select(AuditLog).where(AuditLog.action == "user.created", AuditLog.entity_id == user["id"])
    ).one()
    assert entry.actor_role == "admin"
    assert entry.request_id  # correlated with the HTTP request
    assert entry.ip_hash and entry.ip_hash != "testclient"
    serialised = str(entry.after)
    assert "Plenty-long-pass-77" not in serialised and "argon2" not in serialised


def test_audit_endpoint_filters_and_paginates(
    client: TestClient, auth_headers: AuthHeaders, db: Session
) -> None:
    for i in range(5):
        audit.record(
            db,
            actor=Actor.system(),
            ctx=RequestContext.offline(),
            action="test.page",
            entity_type="widget",
            entity_id=str(i),
        )
    db.flush()
    headers = auth_headers(Role.AUDITOR)

    page1 = client.get("/api/v1/audit-log?entity_type=widget&limit=3", headers=headers).json()
    assert [e["entity_id"] for e in page1["items"]] == ["4", "3", "2"]
    assert page1["next_cursor"] is not None

    page2 = client.get(
        f"/api/v1/audit-log?entity_type=widget&limit=3&cursor={page1['next_cursor']}",
        headers=headers,
    ).json()
    assert [e["entity_id"] for e in page2["items"]] == ["1", "0"]
    assert page2["next_cursor"] is None
