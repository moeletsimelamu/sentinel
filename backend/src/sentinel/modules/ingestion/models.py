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
    LargeBinary,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from sentinel.core.db import Base
from sentinel.core.ids import uuid7


class ImportSource(StrEnum):
    CSV = "csv"
    API = "api"


class BatchStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"  # rows validated; some may have been rejected
    REJECTED = "rejected"  # the whole file was unusable (encoding, columns, size)
    FAILED = "failed"  # processing kept failing; job is dead

    @property
    def is_terminal(self) -> bool:
        return self in {BatchStatus.COMPLETED, BatchStatus.REJECTED, BatchStatus.FAILED}


class IssueOutcome(StrEnum):
    REJECTED = "rejected"  # failed validation, not stored
    DUPLICATE = "duplicate"  # same (source_system, external_id) seen before, not stored
    POSSIBLE_DUPLICATE = "possible_duplicate"  # stored, but content matches another record


class ImportBatch(Base):
    """One upload (CSV) or one REST ingestion request, with its validation summary."""

    __tablename__ = "import_batches"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255))
    file_sha256: Mapped[str | None] = mapped_column(String(64))
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    possible_duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ignored_columns: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    file_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    identical_to_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("import_batches.id", ondelete="SET NULL")
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="SET NULL")
    )
    scoring_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id", ondelete="SET NULL")
    )
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("source IN ('csv', 'api')", name="source_valid"),
        CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'rejected', 'failed')",
            name="status_valid",
        ),
        CheckConstraint(
            "row_count >= 0 AND accepted_count >= 0 AND rejected_count >= 0 "
            "AND duplicate_count >= 0 AND possible_duplicate_count >= 0",
            name="counts_non_negative",
        ),
        Index("ix_import_batches_created_at", "created_at"),
        Index("ix_import_batches_file_sha256", "file_sha256"),
    )


class ImportFile(Base):
    """Raw uploaded bytes, held only until the worker has processed the batch.

    Stored in Postgres so the API and worker containers need no shared volume. Deleted
    after processing (data minimisation); the batch keeps the SHA-256 for traceability.
    """

    __tablename__ = "import_files"

    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), primary_key=True
    )
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ImportRowIssue(Base):
    """A row that was rejected, skipped as a duplicate, or accepted as a possible duplicate.

    ``row_number`` is the line in the CSV file (header = line 1), or the 1-based position
    in a REST request. ``raw_row`` is kept for rejected rows only, after redaction.
    """

    __tablename__ = "import_row_issues"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(64))
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("transactions.id", ondelete="RESTRICT")
    )
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    raw_row: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('rejected', 'duplicate', 'possible_duplicate')", name="outcome_valid"
        ),
        Index("ix_import_row_issues_batch_row", "batch_id", "row_number"),
    )
