from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from sentinel.modules.ingestion.models import BatchStatus, ImportSource, IssueOutcome

SourceSystem = Annotated[
    str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_\-]{1,31}$", strip_whitespace=True)
]


class FileError(BaseModel):
    code: str
    message: str
    details: Any = None


class ImportBatchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: ImportSource
    source_system: str
    filename: str | None
    file_sha256: str | None
    file_size_bytes: int | None
    status: BatchStatus
    row_count: int
    accepted_count: int
    rejected_count: int
    duplicate_count: int
    possible_duplicate_count: int
    ignored_columns: list[str]
    file_error: FileError | None
    identical_to_batch_id: uuid.UUID | None
    uploaded_by_id: uuid.UUID | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ImportBatchPage(BaseModel):
    items: list[ImportBatchOut]
    total: int
    limit: int
    offset: int


class IssueReason(BaseModel):
    field: str | None
    code: str
    message: str
    related_transaction_id: uuid.UUID | None = None


class RowIssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    row_number: int
    outcome: IssueOutcome
    external_id: str | None
    transaction_id: uuid.UUID | None
    reasons: list[IssueReason]
    raw_row: dict[str, Any] | None


class RowIssuePage(BaseModel):
    items: list[RowIssueOut]
    total: int
    limit: int
    offset: int


class ApiIngestRequest(BaseModel):
    """REST ingestion. Records are validated one by one (same rules as CSV rows), so a
    bad record is reported without failing the others."""

    model_config = ConfigDict(extra="forbid")

    source_system: SourceSystem
    records: list[dict[str, Any]] = Field(min_length=1)


class ApiIngestResult(BaseModel):
    batch: ImportBatchOut
    issues: list[RowIssueOut]
