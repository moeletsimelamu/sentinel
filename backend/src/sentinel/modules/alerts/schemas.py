from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sentinel.modules.alerts.models import ResolutionReason
from sentinel.modules.scoring.schemas import AssessmentOut
from sentinel.modules.transactions.schemas import TransactionOut

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=4000)]


class AlertSummary(BaseModel):
    """A work item flagged for review — not a finding about the customer."""

    id: uuid.UUID
    reference: str
    customer_ref: str
    severity: str
    priority_score: int
    status: str
    assignee_id: uuid.UUID | None
    assignee_name: str | None
    transaction_count: int
    resolution_reason: str | None
    opened_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None
    version: int


class AlertPage(BaseModel):
    items: list[AlertSummary]
    next_cursor: str | None


class LinkedTransaction(BaseModel):
    transaction: TransactionOut
    assessment: AssessmentOut


class AlertEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    actor_id: uuid.UUID | None
    from_status: str | None
    to_status: str | None
    payload: dict[str, Any]
    created_at: datetime


class AlertDetail(AlertSummary):
    case_id: uuid.UUID | None
    transactions: list[LinkedTransaction]
    events: list[AlertEventOut]


class VersionedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, description="The alert version you are acting on (409 if stale)")


class AssignRequest(VersionedAction):
    assignee_id: uuid.UUID


class CloseRequest(VersionedAction):
    resolution_reason: ResolutionReason
    note: Text = Field(description="Findings supporting the resolution")


class ReopenRequest(VersionedAction):
    reason: Text


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    corrects_event_id: int | None = None
