"""Alert policy and analyst workflow.

Policy: an assessment is *alertable* when its risk score reaches the active scoring
config's alert threshold or a rule with ``force_alert`` fired. An open alert for the same
customer whose latest activity is within 24 hours absorbs the transaction instead of a
new alert being raised (no alert storms). A transaction belongs to at most one alert.

Workflow (docs/REQUIREMENTS.md §5.2)::

    NEW --claim/assign--> ASSIGNED --start--> IN_REVIEW --close--> CLOSED
     ^                       ^  (reassign)        |                  |
     |                       +--------------------+                  |
     +------------------------- reopen (lead, reason) ---------------+

ESCALATED is entered when a case is opened from the alert (Phase 8).

Every mutation checks the client's ``version`` (409 when stale), writes an append-only
``alert_events`` row and an audit record in the same transaction.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text, tuple_
from sqlalchemy.orm import Session

from sentinel.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationFailedError
from sentinel.core.permissions import Permission
from sentinel.modules.alerts.models import (
    SEVERITY_RANK,
    Alert,
    AlertEvent,
    AlertStatus,
    AlertTransaction,
    ResolutionReason,
    Severity,
)
from sentinel.modules.audit import service as audit
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.auth import repository as users
from sentinel.modules.auth.schemas import Principal
from sentinel.modules.scoring.service import Assessed, ScoringOutcome
from sentinel.modules.transactions.models import Customer

DEDUP_WINDOW = timedelta(hours=24)
OPEN = [s.value for s in AlertStatus if s.is_open]


def _now() -> datetime:
    return datetime.now(UTC)


def _severity(a: Assessed) -> Severity:
    sev = Severity(a.risk_category) if a.risk_category in Severity else Severity.LOW
    if a.force_alert and SEVERITY_RANK[sev] < SEVERITY_RANK[Severity.HIGH]:
        sev = Severity.HIGH  # a forced alert is never shown as low severity
    return sev


def _event(
    db: Session,
    alert: Alert,
    event_type: str,
    *,
    actor_id: uuid.UUID | None,
    from_status: str | None = None,
    to_status: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AlertEvent:
    ev = AlertEvent(
        alert_id=alert.id,
        actor_id=actor_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        payload=payload or {},
    )
    db.add(ev)
    return ev


# ------------------------------------------------------------------- policy


def raise_alerts(db: Session, outcome: ScoringOutcome) -> dict[str, int]:
    """Create or extend alerts for alertable assessments. Runs in the caller's transaction."""
    alertable = sorted(
        (a for a in outcome.assessed if a.alertable),
        key=lambda a: (a.transaction.occurred_at, a.transaction.id),
    )
    stats = {"alerts_created": 0, "alerts_extended": 0, "transactions_linked": 0}
    if not alertable:
        return stats
    txn_ids = [a.transaction.id for a in alertable]
    already = set(
        db.scalars(
            select(AlertTransaction.transaction_id).where(
                AlertTransaction.transaction_id.in_(txn_ids)
            )
        )
    )
    customers = sorted({a.transaction.customer_id for a in alertable})
    open_alerts = db.scalars(
        select(Alert)
        .where(Alert.customer_id.in_(customers), Alert.status.in_(OPEN))
        .order_by(Alert.last_activity_at)
        .with_for_update()
    ).all()
    latest_open: dict[uuid.UUID, Alert] = {a.customer_id: a for a in open_alerts}

    for a in alertable:
        txn = a.transaction
        if txn.id in already:
            continue  # re-scoring never links a transaction twice
        alert = latest_open.get(txn.customer_id)
        sev = _severity(a)
        if alert is not None and abs(txn.occurred_at - alert.last_activity_at) <= DEDUP_WINDOW:
            alert.transaction_count += 1
            alert.last_activity_at = max(alert.last_activity_at, txn.occurred_at)
            alert.priority_score = max(alert.priority_score, a.risk_score)
            if SEVERITY_RANK[sev] > SEVERITY_RANK[Severity(alert.severity)]:
                alert.severity = sev.value
            alert.version += 1
            _event(
                db,
                alert,
                "transaction_linked",
                actor_id=None,
                payload={"transaction_id": str(txn.id), "risk_score": a.risk_score},
            )
            stats["alerts_extended"] += 1
        else:
            seq = db.execute(text("SELECT nextval('alert_reference_seq')")).scalar_one()
            alert = Alert(
                reference=f"ALR-{seq:06d}",
                customer_id=txn.customer_id,
                primary_assessment_id=a.assessment_id,
                severity=sev.value,
                priority_score=a.risk_score,
                status=AlertStatus.NEW,
                transaction_count=1,
                last_activity_at=txn.occurred_at,
                version=1,
            )
            db.add(alert)
            db.flush()
            _event(
                db,
                alert,
                "created",
                actor_id=None,
                to_status=AlertStatus.NEW,
                payload={
                    "transaction_id": str(txn.id),
                    "risk_score": a.risk_score,
                    "forced": a.force_alert,
                },
            )
            latest_open[txn.customer_id] = alert
            stats["alerts_created"] += 1
        db.add(
            AlertTransaction(
                alert_id=alert.id, transaction_id=txn.id, assessment_id=a.assessment_id
            )
        )
        already.add(txn.id)
        stats["transactions_linked"] += 1
    db.flush()
    return stats


# ----------------------------------------------------------------- workflow


def _locked(db: Session, alert_id: uuid.UUID, expected_version: int) -> Alert:
    alert = db.scalar(select(Alert).where(Alert.id == alert_id).with_for_update())
    if alert is None:
        raise NotFoundError("Alert not found")
    if alert.version != expected_version:
        raise ConflictError(
            "This alert changed since you loaded it. Reload and try again.",
            code="stale_version",
            details={"current_version": alert.version},
        )
    return alert


def _require_status(alert: Alert, action: str, allowed: set[AlertStatus]) -> None:
    if AlertStatus(alert.status) not in allowed:
        raise ConflictError(
            f"Cannot {action} an alert that is {alert.status.replace('_', ' ')}",
            code="invalid_transition",
            details={
                "status": alert.status,
                "action": action,
                "allowed_from": sorted(s.value for s in allowed),
            },
        )


def _require_assignee(alert: Alert, by: Principal, action: str) -> None:
    if alert.assignee_id != by.id:
        raise ForbiddenError(
            f"Only the assigned analyst can {action} this alert", code="not_assignee"
        )


def _transition(
    db: Session,
    alert: Alert,
    by: Principal,
    ctx: RequestContext,
    action: str,
    to_status: AlertStatus,
    payload: dict[str, Any] | None = None,
) -> Alert:
    before = {"status": alert.status, "assignee_id": alert.assignee_id}
    from_status = alert.status
    alert.status = to_status
    alert.version += 1
    _event(
        db,
        alert,
        action,
        actor_id=by.id,
        from_status=from_status,
        to_status=to_status,
        payload=payload,
    )
    audit.record(
        db,
        actor=Actor(by.id, by.role.value),
        ctx=ctx,
        action=f"alert.{action}",
        entity_type="alert",
        entity_id=alert.id,
        before=before,
        after={"status": alert.status, "assignee_id": alert.assignee_id, **(payload or {})},
    )
    db.commit()
    db.refresh(alert)
    return alert


def claim(
    db: Session, alert_id: uuid.UUID, version: int, by: Principal, ctx: RequestContext
) -> Alert:
    alert = _locked(db, alert_id, version)
    _require_status(alert, "claim", {AlertStatus.NEW})
    alert.assignee_id = by.id
    return _transition(
        db, alert, by, ctx, "claimed", AlertStatus.ASSIGNED, {"assignee_id": str(by.id)}
    )


def assign(
    db: Session,
    alert_id: uuid.UUID,
    version: int,
    assignee_id: uuid.UUID,
    by: Principal,
    ctx: RequestContext,
) -> Alert:
    alert = _locked(db, alert_id, version)
    _require_status(alert, "assign", {AlertStatus.NEW, AlertStatus.ASSIGNED, AlertStatus.IN_REVIEW})
    target = users.get_user(db, assignee_id)
    if target is None or not target.is_active:
        raise ValidationFailedError("Assignee not found or inactive", code="invalid_assignee")
    if Permission.ALERTS_WORK.value not in target.permissions:
        raise ValidationFailedError(
            "Alerts can only be assigned to analysts", code="invalid_assignee"
        )
    previous = alert.assignee_id
    alert.assignee_id = assignee_id
    return _transition(
        db,
        alert,
        by,
        ctx,
        "assigned",
        AlertStatus.ASSIGNED,
        {
            "assignee_id": str(assignee_id),
            "previous_assignee_id": str(previous) if previous else None,
        },
    )


def start_review(
    db: Session, alert_id: uuid.UUID, version: int, by: Principal, ctx: RequestContext
) -> Alert:
    alert = _locked(db, alert_id, version)
    _require_status(alert, "start review of", {AlertStatus.ASSIGNED})
    _require_assignee(alert, by, "start reviewing")
    return _transition(db, alert, by, ctx, "review_started", AlertStatus.IN_REVIEW)


def close(
    db: Session,
    alert_id: uuid.UUID,
    version: int,
    reason: ResolutionReason,
    note: str,
    by: Principal,
    ctx: RequestContext,
) -> Alert:
    if reason is ResolutionReason.ESCALATED_TO_CASE:
        raise ValidationFailedError(
            "Escalation happens by opening a case from the alert", code="invalid_resolution"
        )
    alert = _locked(db, alert_id, version)
    _require_status(alert, "close", {AlertStatus.IN_REVIEW})
    _require_assignee(alert, by, "close")
    alert.resolution_reason = reason.value
    alert.closed_at = _now()
    return _transition(
        db,
        alert,
        by,
        ctx,
        "closed",
        AlertStatus.CLOSED,
        {"resolution_reason": reason.value, "note": note},
    )


def reopen(
    db: Session,
    alert_id: uuid.UUID,
    version: int,
    reason: str,
    by: Principal,
    ctx: RequestContext,
) -> Alert:
    if Permission.ALERTS_ASSIGN_OTHERS not in by.permissions:
        raise ForbiddenError("Only a lead analyst can reopen alerts", code="lead_required")
    alert = _locked(db, alert_id, version)
    _require_status(alert, "reopen", {AlertStatus.CLOSED})
    previous = {"resolution_reason": alert.resolution_reason}
    alert.assignee_id = None
    alert.resolution_reason = None
    alert.closed_at = None
    return _transition(
        db, alert, by, ctx, "reopened", AlertStatus.NEW, {"reason": reason, **previous}
    )


def add_note(
    db: Session,
    alert_id: uuid.UUID,
    body: str,
    corrects_event_id: int | None,
    by: Principal,
    ctx: RequestContext,
) -> AlertEvent:
    """Notes are append-only; a correction is a new note that references the original."""
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise NotFoundError("Alert not found")
    if corrects_event_id is not None:
        original = db.get(AlertEvent, corrects_event_id)
        if original is None or original.alert_id != alert_id or original.event_type != "note":
            raise ValidationFailedError("The corrected note does not exist on this alert")
    ev = _event(
        db,
        alert,
        "note",
        actor_id=by.id,
        payload={"body": body, "corrects_event_id": corrects_event_id},
    )
    audit.record(
        db,
        actor=Actor(by.id, by.role.value),
        ctx=ctx,
        action="alert.note_added",
        entity_type="alert",
        entity_id=alert.id,
        after={"corrects_event_id": corrects_event_id, "length": len(body)},
    )
    db.commit()
    db.refresh(ev)
    return ev


# -------------------------------------------------------------------- reads


@dataclass(frozen=True)
class AlertFilters:
    statuses: tuple[str, ...] = tuple(OPEN)
    assignee_id: uuid.UUID | None = None
    unassigned: bool = False
    severity: str | None = None
    customer_ref: str | None = None


def encode_cursor(alert: Alert) -> str:
    raw = f"{alert.priority_score}|{alert.opened_at.isoformat()}|{alert.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode()).decode()
        score, opened, aid = raw.split("|", 2)
        return int(score), datetime.fromisoformat(opened), uuid.UUID(aid)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ValidationFailedError("Invalid cursor", code="invalid_cursor") from None


def list_alerts(
    db: Session, f: AlertFilters, *, cursor: str | None, limit: int
) -> tuple[list[Alert], str | None]:
    """Queue order: highest priority first, then oldest first."""
    stmt = select(Alert).where(Alert.status.in_(f.statuses))
    if f.assignee_id is not None:
        stmt = stmt.where(Alert.assignee_id == f.assignee_id)
    if f.unassigned:
        stmt = stmt.where(Alert.assignee_id.is_(None))
    if f.severity:
        stmt = stmt.where(Alert.severity == f.severity)
    if f.customer_ref:
        stmt = stmt.join(Customer, Customer.id == Alert.customer_id).where(
            Customer.external_ref == f.customer_ref
        )
    if cursor:
        score, opened, aid = _decode_cursor(cursor)
        # (priority desc, opened asc, id asc) keyset
        stmt = stmt.where(
            (Alert.priority_score < score)
            | (
                (Alert.priority_score == score)
                & (tuple_(Alert.opened_at, Alert.id) > tuple_(opened, aid))
            )
        )
    stmt = stmt.order_by(Alert.priority_score.desc(), Alert.opened_at, Alert.id).limit(limit + 1)
    rows = list(db.scalars(stmt))
    next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor


def get_alert(db: Session, alert_id: uuid.UUID) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise NotFoundError("Alert not found")
    return alert


def events(db: Session, alert_id: uuid.UUID) -> Sequence[AlertEvent]:
    return db.scalars(
        select(AlertEvent).where(AlertEvent.alert_id == alert_id).order_by(AlertEvent.id)
    ).all()


def names_and_refs(
    db: Session, alerts: Sequence[Alert]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    """Customer refs and assignee names for a page of alerts (two queries, no N+1)."""
    from sentinel.modules.auth.models import User

    cust_ids = {a.customer_id for a in alerts}
    user_ids = {a.assignee_id for a in alerts if a.assignee_id}
    refs: dict[uuid.UUID, str] = {}
    names: dict[uuid.UUID, str] = {}
    if cust_ids:
        stmt = select(Customer.id, Customer.external_ref).where(Customer.id.in_(cust_ids))
        refs = {cid: ref for cid, ref in db.execute(stmt)}
    if user_ids:
        user_stmt = select(User.id, User.full_name).where(User.id.in_(user_ids))
        names = {uid: name for uid, name in db.execute(user_stmt)}
    return refs, names


def linked(db: Session, alert: Alert) -> list[tuple[Any, Any]]:
    """(transaction, assessment) pairs covered by the alert, in time order."""
    from sentinel.modules.scoring.models import RiskAssessment
    from sentinel.modules.transactions.models import Transaction

    rows = db.execute(
        select(Transaction, RiskAssessment)
        .join(AlertTransaction, AlertTransaction.transaction_id == Transaction.id)
        .join(RiskAssessment, RiskAssessment.id == AlertTransaction.assessment_id)
        .where(AlertTransaction.alert_id == alert.id)
        .order_by(Transaction.occurred_at, Transaction.id)
    ).all()
    return [(t, a) for t, a in rows]
