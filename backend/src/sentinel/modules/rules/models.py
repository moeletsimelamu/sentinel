"""Rule configuration is never edited in place.

A change creates a new ``RuleVersion`` and a new ``RuleSet`` snapshot pinning the current
version of every rule. Assessments reference the rule set they used, so any historical
score can be reproduced exactly. All four tables are append-only (database trigger).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sentinel.core.db import Base
from sentinel.core.ids import uuid7


class RuleDefinition(Base):
    __tablename__ = "rule_definitions"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RuleVersion(Base):
    __tablename__ = "rule_versions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    rule_id: Mapped[int] = mapped_column(ForeignKey("rule_definitions.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    weight: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    force_alert: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    rule: Mapped[RuleDefinition] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("rule_id", "version", name="uq_rule_versions_rule_id_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("weight BETWEEN 0 AND 100", name="weight_range"),
        CheckConstraint("severity IN ('high', 'low', 'medium')", name="severity_valid"),
    )


class RuleSet(Base):
    __tablename__ = "rule_sets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    version: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    members: Mapped[list[RuleSetMember]] = relationship(lazy="selectin")


class RuleSetMember(Base):
    __tablename__ = "rule_set_members"

    rule_set_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("rule_sets.id"), primary_key=True)
    rule_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("rule_versions.id"), primary_key=True
    )

    rule_version: Mapped[RuleVersion] = relationship(lazy="joined")
