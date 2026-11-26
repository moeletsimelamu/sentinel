from __future__ import annotations

import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from sentinel.core.db import get_db
from sentinel.core.errors import ValidationFailedError
from sentinel.core.permissions import Permission
from sentinel.modules.alerts import service
from sentinel.modules.alerts.models import Alert, AlertStatus, Severity
from sentinel.modules.alerts.schemas import (
    AlertDetail,
    AlertEventOut,
    AlertPage,
    AlertSummary,
    AssignRequest,
    CloseRequest,
    LinkedTransaction,
    NoteRequest,
    ReopenRequest,
    VersionedAction,
)
from sentinel.modules.audit.context import RequestContext, get_request_context
from sentinel.modules.auth.dependencies import RequirePermission
from sentinel.modules.auth.schemas import Principal
from sentinel.modules.scoring.schemas import AssessmentOut
from sentinel.modules.transactions.schemas import TransactionOut

router = APIRouter(prefix="/alerts", tags=["alerts"])

_read = RequirePermission(Permission.ALERTS_READ)
_work = RequirePermission(Permission.ALERTS_WORK)
_assign_others = RequirePermission(Permission.ALERTS_ASSIGN_OTHERS)


def _summaries(db: Session, alerts: Sequence[Alert]) -> list[AlertSummary]:
    refs, names = service.names_and_refs(db, alerts)
    return [
        AlertSummary(
            id=a.id,
            reference=a.reference,
            customer_ref=refs.get(a.customer_id, "?"),
            severity=a.severity,
            priority_score=a.priority_score,
            status=a.status,
            assignee_id=a.assignee_id,
            assignee_name=names.get(a.assignee_id) if a.assignee_id else None,
            transaction_count=a.transaction_count,
            resolution_reason=a.resolution_reason,
            opened_at=a.opened_at,
            last_activity_at=a.last_activity_at,
            closed_at=a.closed_at,
            version=a.version,
        )
        for a in alerts
    ]


def _detail(db: Session, alert: Alert) -> AlertDetail:
    (summary,) = _summaries(db, [alert])
    return AlertDetail(
        **summary.model_dump(),
        case_id=alert.case_id,
        transactions=[
            LinkedTransaction(
                transaction=TransactionOut.from_model(t),
                assessment=AssessmentOut.model_validate(a),
            )
            for t, a in service.linked(db, alert)
        ],
        events=[AlertEventOut.model_validate(e) for e in service.events(db, alert.id)],
    )


@router.get("", response_model=AlertPage)
def list_alerts(
    status_: list[AlertStatus] | None = Query(default=None, alias="status"),
    assignee: str | None = Query(
        default=None, description="'me', 'unassigned' or a user id", max_length=40
    ),
    severity: Severity | None = None,
    customer_ref: str | None = Query(default=None, max_length=64),
    cursor: str | None = Query(default=None, max_length=300),
    limit: int = Query(default=50, ge=1, le=200),
    principal: Principal = Depends(_read),
    db: Session = Depends(get_db),
) -> AlertPage:
    """The alert queue: open alerts by default, highest priority first, then oldest."""
    assignee_id, unassigned = None, False
    if assignee == "me":
        assignee_id = principal.id
    elif assignee == "unassigned":
        unassigned = True
    elif assignee:
        try:
            assignee_id = uuid.UUID(assignee)
        except ValueError:
            raise ValidationFailedError(
                "assignee must be 'me', 'unassigned' or a user id"
            ) from None
    filters = service.AlertFilters(
        statuses=tuple(s.value for s in status_) if status_ else tuple(service.OPEN),
        assignee_id=assignee_id,
        unassigned=unassigned,
        severity=severity.value if severity else None,
        customer_ref=customer_ref,
    )
    rows, next_cursor = service.list_alerts(db, filters, cursor=cursor, limit=limit)
    return AlertPage(items=_summaries(db, rows), next_cursor=next_cursor)


@router.get("/{alert_id}", response_model=AlertDetail, dependencies=[Depends(_read)])
def get_alert(alert_id: uuid.UUID, db: Session = Depends(get_db)) -> AlertDetail:
    """Alert with every covered transaction, its assessment (reasons, rule hits, model
    contributions) and the full timeline."""
    return _detail(db, service.get_alert(db, alert_id))


@router.post("/{alert_id}/claim", response_model=AlertDetail)
def claim_alert(
    alert_id: uuid.UUID,
    body: VersionedAction,
    by: Principal = Depends(_work),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> AlertDetail:
    return _detail(db, service.claim(db, alert_id, body.version, by, ctx))


@router.post("/{alert_id}/assign", response_model=AlertDetail)
def assign_alert(
    alert_id: uuid.UUID,
    body: AssignRequest,
    by: Principal = Depends(_assign_others),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> AlertDetail:
    """Assign to any analyst (admins and lead analysts)."""
    return _detail(db, service.assign(db, alert_id, body.version, body.assignee_id, by, ctx))


@router.post("/{alert_id}/start", response_model=AlertDetail)
def start_review(
    alert_id: uuid.UUID,
    body: VersionedAction,
    by: Principal = Depends(_work),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> AlertDetail:
    return _detail(db, service.start_review(db, alert_id, body.version, by, ctx))


@router.post("/{alert_id}/close", response_model=AlertDetail)
def close_alert(
    alert_id: uuid.UUID,
    body: CloseRequest,
    by: Principal = Depends(_work),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> AlertDetail:
    return _detail(
        db,
        service.close(db, alert_id, body.version, body.resolution_reason, body.note, by, ctx),
    )


@router.post("/{alert_id}/reopen", response_model=AlertDetail)
def reopen_alert(
    alert_id: uuid.UUID,
    body: ReopenRequest,
    by: Principal = Depends(_work),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> AlertDetail:
    """Lead analysts only (checked in the service): back to NEW with a reason."""
    return _detail(db, service.reopen(db, alert_id, body.version, body.reason, by, ctx))


@router.post("/{alert_id}/notes", response_model=AlertEventOut, status_code=status.HTTP_201_CREATED)
def add_note(
    alert_id: uuid.UUID,
    body: NoteRequest,
    by: Principal = Depends(_work),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> AlertEventOut:
    ev = service.add_note(db, alert_id, body.body, body.corrects_event_id, by, ctx)
    return AlertEventOut.model_validate(ev)
