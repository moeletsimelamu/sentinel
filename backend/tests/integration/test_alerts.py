"""Alert policy (thresholds, force-alert, 24h de-duplication) and the analyst workflow."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from sentinel.core.permissions import Permission, Role
from sentinel.modules.audit.models import AuditLog
from tests.conftest import MakeUser, bearer, login
from tests.integration.test_ingestion import run_worker

pytestmark = pytest.mark.integration

JHB = {"city": "Johannesburg", "country": "ZA", "lat": -26.2041, "lon": 28.0473}
LON = {"city": "London", "country": "GB", "lat": 51.5074, "lon": -0.1278}
T0 = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)


def record(ext: str, cust: str, when: datetime, place: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return {
        "external_id": ext,
        "customer_ref": cust,
        "occurred_at": when.isoformat().replace("+00:00", "Z"),
        "amount": kw.pop("amount", "120.00"),
        "currency": "ZAR",
        "txn_type": "purchase",
        "channel": "card_present",
        "merchant_category": kw.pop("category", "groceries"),
        "status": "completed",
        **place,
        **kw,
    }


def travel_pair(prefix: str, cust: str, start: datetime) -> list[dict[str, Any]]:
    """A purchase at home, then one in London 30 minutes later: IMPOSSIBLE_TRAVEL (force)."""
    return [
        record(f"{prefix}-H", cust, start, JHB),
        record(f"{prefix}-L", cust, start + timedelta(minutes=30), LON),
    ]


@pytest.fixture
def people(make_user: MakeUser, client: TestClient) -> dict[str, Any]:
    users = {
        "analyst": make_user(Role.ANALYST),
        "other": make_user(Role.ANALYST),
        "lead": make_user(Role.ANALYST, grants=(Permission.ALERTS_ASSIGN_OTHERS,)),
        "admin": make_user(Role.ADMIN),
        "auditor": make_user(Role.AUDITOR),
    }
    out: dict[str, Any] = {k: u for k, u in users.items()}
    out["h"] = {k: bearer(login(client, u.email)) for k, u in users.items()}
    return out


def ingest(
    client: TestClient, headers: dict[str, str], records: list[dict[str, Any]], db: Session
) -> None:
    resp = client.post(
        "/api/v1/transactions",
        json={"source_system": "alerts_test", "records": records},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["batch"]["rejected_count"] == 0, resp.text
    run_worker(db)


def alerts(client: TestClient, headers: dict[str, str], **params: Any) -> list[dict[str, Any]]:
    resp = client.get("/api/v1/alerts", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    items: list[dict[str, Any]] = resp.json()["items"]
    return items


@pytest.fixture
def one_alert(client: TestClient, people: dict[str, Any], db: Session) -> dict[str, Any]:
    ingest(client, people["h"]["analyst"], travel_pair("A1", "C-AL-1", T0), db)
    (alert,) = alerts(client, people["h"]["analyst"], customer_ref="C-AL-1")
    return alert


def act(
    client: TestClient, headers: dict[str, str], alert: dict[str, Any], action: str, **body: Any
) -> Any:
    return client.post(
        f"/api/v1/alerts/{alert['id']}/{action}",
        json={"version": alert["version"], **body},
        headers=headers,
    )


# -------------------------------------------------------------------- policy


def test_force_alert_rule_raises_a_high_severity_alert(one_alert: dict[str, Any]) -> None:
    assert one_alert["status"] == "new" and one_alert["severity"] == "high"
    assert one_alert["reference"].startswith("ALR-") and one_alert["transaction_count"] == 1
    assert one_alert["assignee_id"] is None


def test_alert_detail_explains_the_flag(
    client: TestClient, people: dict[str, Any], one_alert: dict[str, Any]
) -> None:
    detail = client.get(f"/api/v1/alerts/{one_alert['id']}", headers=people["h"]["auditor"]).json()
    (linked,) = detail["transactions"]
    assert linked["transaction"]["external_id"] == "A1-L"
    assessment = linked["assessment"]
    assert assessment["force_alert"] is True
    assert any("IMPOSSIBLE_TRAVEL" in r for r in assessment["reasons"])
    assert [e["event_type"] for e in detail["events"]] == ["created"]


def test_same_customer_within_24h_is_one_alert_later_is_new(
    client: TestClient, people: dict[str, Any], db: Session
) -> None:
    records = (
        travel_pair("D1", "C-AL-2", T0)
        + travel_pair("D2", "C-AL-2", T0 + timedelta(hours=5))  # absorbed
        + travel_pair("D3", "C-AL-2", T0 + timedelta(days=3))  # new alert
    )
    ingest(client, people["h"]["analyst"], records, db)
    found = sorted(
        alerts(client, people["h"]["analyst"], customer_ref="C-AL-2"), key=lambda a: a["opened_at"]
    )
    # D2-H (back in Johannesburg 4.5 h after London) is itself impossible travel, so the
    # first alert absorbs D1-L, D2-H and D2-L; D3 is three days later and opens a new alert.
    assert [a["transaction_count"] for a in found] == [3, 1]
    first = client.get(f"/api/v1/alerts/{found[0]['id']}", headers=people["h"]["analyst"]).json()
    events = [e["event_type"] for e in first["events"]]
    assert events == ["created", "transaction_linked", "transaction_linked"]
    assert first["version"] == 3


def test_threshold_alerts_and_rescoring_does_not_duplicate(
    client: TestClient, people: dict[str, Any], db: Session
) -> None:
    resp = client.post(
        "/api/v1/scoring-configs",
        json={
            "expected_version": 1,
            "w_rule": 0.5,
            "rule_cap": 100,
            "bands": [
                {"name": "low", "min_score": 0},
                {"name": "medium", "min_score": 20},
                {"name": "high", "min_score": 30},
            ],
            "alert_threshold": 30,
            "change_reason": "Lower the threshold for this test",
        },
        headers=people["h"]["admin"],
    )
    assert resp.status_code == 201, resp.text
    big = record("BIG-1", "C-AL-3", T0, JHB, amount="60000.00", category="electronics")
    ingest(client, people["h"]["analyst"], [big], db)
    (alert,) = alerts(client, people["h"]["analyst"], customer_ref="C-AL-3")
    assert alert["priority_score"] == 30 and alert["severity"] == "high"
    batch_id = db.execute(
        text("SELECT batch_id FROM transactions WHERE external_id = 'BIG-1'")
    ).scalar_one()
    run = client.post(
        "/api/v1/detection/runs", json={"batch_id": str(batch_id)}, headers=people["h"]["analyst"]
    )
    assert run.status_code == 202
    run_worker(db)
    (again,) = alerts(client, people["h"]["analyst"], customer_ref="C-AL-3")
    assert again["transaction_count"] == 1  # re-scoring never links a transaction twice


def test_below_threshold_raises_nothing(
    client: TestClient, people: dict[str, Any], db: Session
) -> None:
    ingest(client, people["h"]["analyst"], [record("Q-1", "C-AL-4", T0, JHB)], db)
    assert alerts(client, people["h"]["analyst"], customer_ref="C-AL-4") == []


# ------------------------------------------------------------------ workflow


def test_full_lifecycle_with_events_and_audit(
    client: TestClient, people: dict[str, Any], one_alert: dict[str, Any], db: Session
) -> None:
    h = people["h"]["analyst"]
    a = act(client, h, one_alert, "claim").json()
    assert a["status"] == "assigned" and a["assignee_id"] == str(people["analyst"].id)
    a = act(client, h, a, "start").json()
    assert a["status"] == "in_review"
    note = client.post(
        f"/api/v1/alerts/{a['id']}/notes",
        json={"body": "Customer travel confirmed by booking"},
        headers=h,
    )
    assert note.status_code == 201
    closed = act(
        client, h, a, "close",
        resolution_reason="legitimate_customer_behaviour",
        note="Travel to London was booked a week earlier; behaviour consistent.",
    )  # fmt: skip
    assert closed.status_code == 200, closed.text
    c = closed.json()
    assert (
        c["status"] == "closed"
        and c["closed_at"]
        and c["resolution_reason"] == "legitimate_customer_behaviour"
    )
    assert [e["event_type"] for e in c["events"]] == [
        "created",
        "claimed",
        "review_started",
        "note",
        "closed",
    ]
    actions = db.scalars(
        select(AuditLog.action).where(AuditLog.entity_id == a["id"]).order_by(AuditLog.id)
    ).all()
    assert actions == ["alert.claimed", "alert.review_started", "alert.note_added", "alert.closed"]
    assert alerts(client, h, customer_ref="C-AL-1") == []  # closed alerts leave the open queue
    assert len(alerts(client, h, customer_ref="C-AL-1", status="closed")) == 1


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("start", {}),
        (
            "close",
            {
                "resolution_reason": "false_positive_explained",
                "note": "Nothing unusual found here.",
            },
        ),
        ("reopen", {"reason": "Reopening for a second look"}),
    ],
)
def test_illegal_transitions_from_new(
    client: TestClient,
    people: dict[str, Any],
    one_alert: dict[str, Any],
    action: str,
    body: dict[str, Any],
) -> None:
    h = people["h"]["lead"]  # lead has every permission, so only the state machine refuses
    resp = act(client, h, one_alert, action, **body)
    assert resp.status_code == 409, resp.text
    err = resp.json()["error"]
    assert err["code"] == "invalid_transition" and err["details"]["status"] == "new"


def test_cannot_claim_twice_and_stale_versions_conflict(
    client: TestClient, people: dict[str, Any], one_alert: dict[str, Any]
) -> None:
    first = act(client, people["h"]["analyst"], one_alert, "claim")
    assert first.status_code == 200
    stale = act(client, people["h"]["other"], one_alert, "claim")  # still holds version 1
    assert stale.status_code == 409
    assert stale.json()["error"]["details"] == {"current_version": 2}
    fresh = act(client, people["h"]["other"], first.json(), "claim")
    assert fresh.status_code == 409 and fresh.json()["error"]["code"] == "invalid_transition"


def test_only_the_assignee_can_review_or_close(
    client: TestClient, people: dict[str, Any], one_alert: dict[str, Any]
) -> None:
    a = act(client, people["h"]["analyst"], one_alert, "claim").json()
    resp = act(client, people["h"]["other"], a, "start")
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "not_assignee"


def test_escalation_is_not_a_close_reason(
    client: TestClient, people: dict[str, Any], one_alert: dict[str, Any]
) -> None:
    h = people["h"]["analyst"]
    a = act(client, h, act(client, h, one_alert, "claim").json(), "start").json()
    resp = act(
        client,
        h,
        a,
        "close",
        resolution_reason="escalated_to_case",
        note="Needs a full investigation.",
    )
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "invalid_resolution"


def test_close_requires_findings(
    client: TestClient, people: dict[str, Any], one_alert: dict[str, Any]
) -> None:
    h = people["h"]["analyst"]
    a = act(client, h, act(client, h, one_alert, "claim").json(), "start").json()
    assert (
        act(client, h, a, "close", resolution_reason="duplicate_alert", note="dup").status_code
        == 422
    )


def test_reopen_is_lead_only(
    client: TestClient, people: dict[str, Any], one_alert: dict[str, Any]
) -> None:
    h = people["h"]["analyst"]
    a = act(client, h, act(client, h, one_alert, "claim").json(), "start").json()
    c = act(
        client,
        h,
        a,
        "close",
        resolution_reason="false_positive_explained",
        note="Benign: card used by owner.",
    ).json()
    denied = act(client, h, c, "reopen", reason="I changed my mind about this")
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "lead_required"
    reopened = act(
        client, people["h"]["lead"], c, "reopen", reason="Quality review found missing checks"
    )
    assert reopened.status_code == 200
    r = reopened.json()
    assert r["status"] == "new" and r["assignee_id"] is None and r["resolution_reason"] is None


def test_assignment_by_admin_and_lead(
    client: TestClient, people: dict[str, Any], one_alert: dict[str, Any]
) -> None:
    resp = act(
        client, people["h"]["admin"], one_alert, "assign", assignee_id=str(people["other"].id)
    )
    assert resp.status_code == 200 and resp.json()["assignee_id"] == str(people["other"].id)
    bad = act(
        client, people["h"]["lead"], resp.json(), "assign", assignee_id=str(people["auditor"].id)
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_assignee"
    plain = act(
        client, people["h"]["analyst"], resp.json(), "assign", assignee_id=str(people["analyst"].id)
    )
    assert plain.status_code == 403  # self-service is "claim"; assigning others is lead-only


def test_admins_and_auditors_cannot_work_alerts(
    client: TestClient, people: dict[str, Any], one_alert: dict[str, Any]
) -> None:
    for who in ("admin", "auditor"):
        assert act(client, people["h"][who], one_alert, "claim").status_code == 403
    assert (
        client.get(f"/api/v1/alerts/{one_alert['id']}", headers=people["h"]["auditor"]).status_code
        == 200
    )


def test_notes_are_append_only_with_corrections(
    client: TestClient, people: dict[str, Any], one_alert: dict[str, Any], db: Session
) -> None:
    h = people["h"]["analyst"]
    first = client.post(
        f"/api/v1/alerts/{one_alert['id']}/notes", json={"body": "Called the custmer"}, headers=h
    ).json()
    fix = client.post(
        f"/api/v1/alerts/{one_alert['id']}/notes",
        json={"body": "Typo: called the customer", "corrects_event_id": first["id"]},
        headers=h,
    )
    assert fix.status_code == 201 and fix.json()["payload"]["corrects_event_id"] == first["id"]
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text("UPDATE alert_events SET payload = '{}'::jsonb"))
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text("DELETE FROM alert_transactions"))


def test_queue_order_filters_and_pagination(
    client: TestClient, people: dict[str, Any], db: Session
) -> None:
    records = []
    for i in range(5):
        records += travel_pair(f"P{i}", f"C-AL-Q{i}", T0 + timedelta(days=i))
    ingest(client, people["h"]["analyst"], records, db)
    h = people["h"]["analyst"]
    queue = [a for a in alerts(client, h, limit=200) if a["customer_ref"].startswith("C-AL-Q")]
    assert len(queue) == 5
    keys = [(-a["priority_score"], a["opened_at"]) for a in queue]
    assert keys == sorted(keys)
    act(client, h, queue[0], "claim")
    assert [a["id"] for a in alerts(client, h, assignee="me")] == [queue[0]["id"]]
    assert queue[0]["id"] not in {
        a["id"] for a in alerts(client, h, assignee="unassigned", limit=200)
    }

    seen, cursor = [], None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = client.get("/api/v1/alerts", params=params, headers=h).json()
        seen += [a["id"] for a in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) and {a["id"] for a in queue} <= set(seen)
    assert client.get("/api/v1/alerts", params={"assignee": "nobody"}, headers=h).status_code == 422
    assert client.get(f"/api/v1/alerts/{uuid.uuid4()}", headers=h).status_code == 404
