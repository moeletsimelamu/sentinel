"""Alerts: system-generated work items, never findings.

``alerts`` is the only mutable table here and carries ``version`` for optimistic locking.
Every change (creation, linked transaction, assignment, transition, note) is an
append-only ``alert_events`` row, so the timeline cannot be rewritten.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sentinel.core.db import Base
from sentinel.core.ids import uuid7


class AlertStatus(StrEnum):
    NEW = "new"
    ASSIGNED = "assigned"
    IN_REVIEW = "in_review"
    CLOSED = "closed"
    ESCALATED = "escalated"  # linked to an investigation case (Phase 8)

    @property
    def is_open(self) -> bool:
        return self in {AlertStatus.NEW, AlertStatus.ASSIGNED, AlertStatus.IN_REVIEW}


class ResolutionReason(StrEnum):
    FALSE_POSITIVE_EXPLAINED = "false_positive_explained"
    LEGITIMATE_CUSTOMER_BEHAVIOUR = "legitimate_customer_behaviour"
    DUPLICATE_ALERT = "duplicate_alert"
    INSUFFICIENT_INFORMATION = "insufficient_information"
    ESCALATED_TO_CASE = "escalated_to_case"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_RANK = {s: i for i, s in enumerate(Severity)}


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    reference: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"), nullable=False)
    primary_assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_assessments.id"), nullable=False
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    priority_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=AlertStatus.NEW)
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    case_id: Mapped[uuid.UUID | None] = mapped_column()  # FK added with cases (Phase 8)
    resolution_reason: Mapped[str | None] = mapped_column(String(40))
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    links: Mapped[list[AlertTransaction]] = relationship(lazy="selectin")

    __table_args__ = (
        CheckConstraint(
            "status IN ('assigned', 'closed', 'escalated', 'in_review', 'new')",
            name="status_valid",
        ),
        CheckConstraint("severity IN ('critical', 'high', 'low', 'medium')", name="severity_valid"),
        CheckConstraint(
            "resolution_reason IS NULL OR resolution_reason IN ('duplicate_alert', "
            "'escalated_to_case', 'false_positive_explained', 'insufficient_information', "
            "'legitimate_customer_behaviour')",
            name="resolution_valid",
        ),
        CheckConstraint(
            "(status IN ('closed', 'escalated')) = "
            "(resolution_reason IS NOT NULL AND closed_at IS NOT NULL)",
            name="terminal_has_resolution",
        ),
        CheckConstraint(
            "status NOT IN ('assigned', 'in_review') OR assignee_id IS NOT NULL",
            name="worked_has_assignee",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        Index("ix_alerts_queue", "status", "priority_score", "opened_at"),
        Index(
            "ix_alerts_open_assignee",
            "assignee_id",
            postgresql_where=text("status IN ('new', 'assigned', 'in_review')"),
        ),
        Index("ix_alerts_customer_status", "customer_id", "status"),
    )


class AlertTransaction(Base):
    """A transaction covered by an alert. A transaction belongs to at most one alert."""

    __tablename__ = "alert_transactions"

    alert_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alerts.id"), primary_key=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id"), primary_key=True, unique=True
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_assessments.id"), nullable=False
    )
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AlertEvent(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    alert_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("alerts.id"), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str | None] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_alert_events_alert_time", "alert_id", "created_at"),)
