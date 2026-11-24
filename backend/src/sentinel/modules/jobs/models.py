from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.core.db import Base
from sentinel.core.ids import uuid7


class JobStatus(StrEnum):
    QUEUED = "queued"  # waiting (first run or a retry after backoff)
    RUNNING = "running"  # claimed by a worker
    SUCCEEDED = "succeeded"
    DEAD = "dead"  # failed max_attempts times; needs a human


class ProcessingJob(Base):
    """A durable unit of background work, claimed with ``FOR UPDATE SKIP LOCKED``."""

    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=JobStatus.QUEUED)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'dead')", name="status_valid"
        ),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts_valid"),
        Index(
            "ix_processing_jobs_claimable",
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
        Index("ix_processing_jobs_status", "status", "created_at"),
    )
