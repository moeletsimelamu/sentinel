from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sentinel.core.db import Base
from sentinel.core.ids import uuid7


class ModelStatus(StrEnum):
    REGISTERED = "registered"
    ACTIVE = "active"
    RETIRED = "retired"


class ModelFamily(StrEnum):
    LOGREG = "logreg"
    RANDOM_FOREST = "random_forest"
    ISOLATION_FOREST = "isolation_forest"
    RULES_BASELINE = "rules_baseline"


class ModelVersion(Base):
    """A trained model (or the rule baseline) registered from its manifest.

    Registration verifies the artefact hash and feature schema but never unpickles the
    artefact. Activation (Phase 7) is a separate, audited admin action.
    """

    __tablename__ = "model_versions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    family: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=ModelStatus.REGISTERED)
    artefact_path: Mapped[str | None] = mapped_column(String(300))
    artefact_sha256: Mapped[str | None] = mapped_column(String(64))
    feature_schema_hash: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_set_version: Mapped[str] = mapped_column(String(32), nullable=False)
    training_dataset_version: Mapped[str] = mapped_column(String(80), nullable=False)
    selected_by_experiment: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    activated_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    evaluations: Mapped[list[ModelEvaluation]] = relationship(
        lazy="selectin", order_by="ModelEvaluation.split"
    )

    __table_args__ = (
        CheckConstraint(
            "family IN ('isolation_forest', 'logreg', 'random_forest', 'rules_baseline')",
            name="family_valid",
        ),
        CheckConstraint("status IN ('active', 'registered', 'retired')", name="status_valid"),
        CheckConstraint(
            "family = 'rules_baseline' OR (artefact_path IS NOT NULL AND artefact_sha256 IS NOT NULL)",
            name="artefact_required",
        ),
        Index(
            "uq_model_versions_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )


class ModelEvaluation(Base):
    """Metrics for one model on one split at one threshold. Append-only."""

    __tablename__ = "model_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_versions.id"), nullable=False
    )
    dataset_version: Mapped[str] = mapped_column(String(80), nullable=False)
    split: Mapped[str] = mapped_column(String(16), nullable=False)
    threshold_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    threshold: Mapped[float | None] = mapped_column(Double)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    ci95: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    curves: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "split IN ('holdout', 'test', 'validation')", name="split_valid"
        ),
        Index("ix_model_evaluations_model", "model_version_id", "split"),
    )
