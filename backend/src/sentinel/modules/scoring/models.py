from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sentinel.core.db import Base
from sentinel.core.ids import uuid7


class ScoringConfig(Base):
    """Versioned scoring weights, bands and alert threshold. Exactly one is active.

    Phase 4 seeds version 1; the admin API to change it arrives with alerts in Phase 7.
    """

    __tablename__ = "scoring_configs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    version: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    w_rule: Mapped[float] = mapped_column(Numeric(4, 3, asdecimal=False), nullable=False)
    w_model: Mapped[float] = mapped_column(Numeric(4, 3, asdecimal=False), nullable=False)
    rule_cap: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    bands: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    alert_threshold: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "w_rule BETWEEN 0 AND 1 AND w_model BETWEEN 0 AND 1 AND w_rule + w_model = 1",
            name="weights_valid",
        ),
        CheckConstraint("rule_cap BETWEEN 1 AND 100", name="rule_cap_range"),
        CheckConstraint("alert_threshold BETWEEN 0 AND 100", name="alert_threshold_range"),
        Index(
            "uq_scoring_configs_one_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )


class RiskAssessment(Base):
    """The combined, versioned result of scoring one transaction. Append-only:
    re-scoring adds a new row; the latest is found by ``assessed_at``."""

    __tablename__ = "risk_assessments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    transaction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    rule_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    model_score: Mapped[float | None] = mapped_column(Double)
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_versions.id"))
    risk_score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    risk_category: Mapped[str] = mapped_column(String(16), nullable=False)
    force_alert: Mapped[bool] = mapped_column(Boolean, nullable=False)
    scoring_config_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("scoring_configs.id"), nullable=False
    )
    rule_set_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rule_sets.id"), nullable=False)
    feature_set_version: Mapped[str] = mapped_column(String(32), nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column()  # no FK: jobs may be pruned
    assessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    hits: Mapped[list[RuleHitRow]] = relationship(lazy="selectin", order_by="RuleHitRow.id")

    __table_args__ = (
        CheckConstraint("mode IN ('combined', 'rules_only')", name="mode_valid"),
        CheckConstraint("risk_score BETWEEN 0 AND 100", name="risk_score_range"),
        CheckConstraint("rule_score BETWEEN 0 AND 100", name="rule_score_range"),
        Index("ix_risk_assessments_txn_time", "transaction_id", "assessed_at"),
        Index("ix_risk_assessments_assessed_at", "assessed_at", postgresql_using="brin"),
    )


class RuleHitRow(Base):
    """One triggered rule within an assessment: observed value(s) vs threshold(s)."""

    __tablename__ = "rule_hits"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_assessments.id"), nullable=False
    )
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rule_versions.id"), nullable=False
    )
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False)
    weight_applied: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    force_alert: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    threshold: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        Index("ix_rule_hits_assessment_id", "assessment_id"),
        Index("ix_rule_hits_rule_version_id", "rule_version_id"),
        Index("ix_rule_hits_rule_code", "rule_code"),
    )
