"""Import workflows: CSV upload (async via the job queue), REST ingestion (synchronous),
import history and the row-issue report. Every write is audited in the same transaction.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from sentinel.core.config import Settings
from sentinel.core.errors import NotFoundError, ValidationFailedError
from sentinel.core.logging import get_logger
from sentinel.modules.audit import service as audit
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.auth.schemas import Principal
from sentinel.modules.ingestion.csv_reader import FileRejectedError, parse_csv
from sentinel.modules.ingestion.models import (
    BatchStatus,
    ImportBatch,
    ImportFile,
    ImportRowIssue,
    ImportSource,
    IssueOutcome,
)
from sentinel.modules.ingestion.pipeline import IncomingRow, IngestOutcome, ingest_rows
from sentinel.modules.ingestion.validation import (
    KNOWN_FIELDS,
    FieldError,
    is_forbidden_field,
    normalise_field_name,
)
from sentinel.modules.jobs import service as jobs

log = get_logger(__name__)

INGEST_CSV_JOB = "ingest_csv"
_UNSAFE_FILENAME_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _now() -> datetime:
    return datetime.now(UTC)


def _actor(principal: Principal) -> Actor:
    return Actor(principal.id, principal.role.value)


def safe_filename(name: str | None) -> str | None:
    """Keep only the final path component, without control characters (display only)."""
    if not name:
        return None
    base = PurePosixPath(name.replace("\\", "/")).name
    base = _UNSAFE_FILENAME_CHARS.sub("", base).strip()
    return base[:255] or None


def _summary(batch: ImportBatch) -> dict[str, Any]:
    return {
        "status": batch.status,
        "source": batch.source,
        "source_system": batch.source_system,
        "row_count": batch.row_count,
        "accepted_count": batch.accepted_count,
        "rejected_count": batch.rejected_count,
        "duplicate_count": batch.duplicate_count,
        "possible_duplicate_count": batch.possible_duplicate_count,
    }


# ------------------------------------------------------------------ CSV upload


def create_csv_import(
    db: Session,
    settings: Settings,
    *,
    content: bytes,
    filename: str | None,
    source_system: str,
    by: Principal,
    ctx: RequestContext,
) -> ImportBatch:
    """Store the upload and queue it. Validation happens in the worker."""
    digest = hashlib.sha256(content).hexdigest()
    previous = db.scalars(
        select(ImportBatch.id)
        .where(
            ImportBatch.file_sha256 == digest,
            ImportBatch.source_system == source_system,
            ImportBatch.status == BatchStatus.COMPLETED,
        )
        .order_by(ImportBatch.created_at.desc())
        .limit(1)
    ).first()
    batch = ImportBatch(
        source=ImportSource.CSV,
        source_system=source_system,
        filename=safe_filename(filename),
        file_sha256=digest,
        file_size_bytes=len(content),
        status=BatchStatus.QUEUED,
        ignored_columns=[],
        identical_to_batch_id=previous,
        uploaded_by_id=by.id,
    )
    db.add(batch)
    db.flush()
    db.add(ImportFile(batch_id=batch.id, content=content))
    job = jobs.enqueue(db, INGEST_CSV_JOB, {"batch_id": str(batch.id)}, settings=settings)
    batch.job_id = job.id
    audit.record(
        db,
        actor=_actor(by),
        ctx=ctx,
        action="import.uploaded",
        entity_type="import_batch",
        entity_id=batch.id,
        after={
            "filename": batch.filename,
            "file_sha256": digest,
            "file_size_bytes": len(content),
            "source_system": source_system,
            "identical_to_batch_id": previous,
        },
    )
    db.commit()
    log.info("import_queued", batch_id=str(batch.id), size_bytes=len(content))
    return batch


def process_csv_batch(db: Session, settings: Settings, payload: dict[str, Any]) -> None:
    """Job handler for ``ingest_csv``. Safe to retry: all row work happens in one
    transaction, and a batch already in a terminal state is left alone."""
    batch_id = uuid.UUID(str(payload["batch_id"]))
    batch = db.get(ImportBatch, batch_id, with_for_update=True)
    if batch is None or BatchStatus(batch.status).is_terminal:
        log.info("import_skipped", batch_id=str(batch_id))
        db.rollback()
        return
    file = db.get(ImportFile, batch_id)
    if file is None:
        _finish(db, batch, BatchStatus.FAILED, {"code": "file_missing", "message": "Upload lost"})
        return

    batch.status = BatchStatus.PROCESSING
    batch.started_at = _now()
    db.commit()  # make progress visible to the API while rows are processed

    try:
        parsed = parse_csv(file.content, max_rows=settings.import_max_rows)
    except FileRejectedError as exc:
        _finish(db, batch, BatchStatus.REJECTED, exc.as_dict(), purge=file)
        return

    batch.ignored_columns = parsed.ignored_columns
    rows = [
        IncomingRow(
            r.line_number,
            r.values,
            (FieldError(None, "malformed_row", r.shape_error),) if r.shape_error else (),
        )
        for r in parsed.rows
    ]
    ingest_rows(db, batch, rows, settings=settings, now=_now())
    _finish(db, batch, BatchStatus.COMPLETED, None, purge=file)


def fail_csv_batch(db: Session, payload: dict[str, Any], error: str) -> None:
    """Called when the ingest job has exhausted its retries."""
    batch = db.get(ImportBatch, uuid.UUID(str(payload["batch_id"])), with_for_update=True)
    if batch is None or BatchStatus(batch.status).is_terminal:
        return
    file = db.get(ImportFile, batch.id)
    _finish(
        db,
        batch,
        BatchStatus.FAILED,
        {"code": "processing_failed", "message": "The import could not be processed"},
        purge=file,
        extra={"last_error": error},
    )


def _finish(
    db: Session,
    batch: ImportBatch,
    status: BatchStatus,
    file_error: dict[str, Any] | None,
    *,
    purge: ImportFile | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    batch.status = status
    batch.file_error = file_error
    batch.finished_at = _now()
    if purge is not None:
        db.delete(purge)  # raw upload no longer needed
    action = {
        BatchStatus.COMPLETED: "import.completed",
        BatchStatus.REJECTED: "import.rejected",
        BatchStatus.FAILED: "import.failed",
    }[status]
    after = {**_summary(batch), **({"file_error": file_error} if file_error else {})}
    audit.record(
        db,
        actor=Actor.system(),
        ctx=RequestContext.offline(),
        action=action,
        entity_type="import_batch",
        entity_id=batch.id,
        after={**after, **(extra or {})},
    )
    db.commit()
    log.info(action.replace(".", "_"), batch_id=str(batch.id), **_summary(batch))


# --------------------------------------------------------------- REST ingest


def ingest_api_records(
    db: Session,
    settings: Settings,
    *,
    source_system: str,
    records: Sequence[dict[str, Any]],
    by: Principal,
    ctx: RequestContext,
) -> tuple[ImportBatch, IngestOutcome]:
    if len(records) > settings.api_ingest_max_records:
        raise ValidationFailedError(
            f"At most {settings.api_ingest_max_records} records per request",
            code="too_many_records",
        )
    now = _now()
    ignored: set[str] = set()
    rows: list[IncomingRow] = []
    for position, record in enumerate(records, start=1):
        values: dict[str, Any] = {}
        errors: list[FieldError] = []
        for key, value in record.items():
            norm = normalise_field_name(key)
            if is_forbidden_field(key):
                # The value is dropped here and never stored or echoed back.
                errors.append(
                    FieldError(
                        norm[:64],
                        "forbidden_field",
                        "Card, bank-credential and personal data fields are not accepted",
                    )
                )
            elif norm in KNOWN_FIELDS:
                values[norm] = value
            else:
                ignored.add(norm[:64])
        rows.append(IncomingRow(position, values, tuple(errors)))

    batch = ImportBatch(
        source=ImportSource.API,
        source_system=source_system,
        status=BatchStatus.PROCESSING,
        ignored_columns=sorted(ignored),
        uploaded_by_id=by.id,
        started_at=now,
    )
    db.add(batch)
    db.flush()
    outcome = ingest_rows(db, batch, rows, settings=settings, now=now)
    batch.status = BatchStatus.COMPLETED
    batch.finished_at = _now()
    audit.record(
        db,
        actor=_actor(by),
        ctx=ctx,
        action="import.completed",
        entity_type="import_batch",
        entity_id=batch.id,
        after=_summary(batch),
    )
    db.commit()
    return batch, outcome


# --------------------------------------------------------------------- reads


def list_batches(
    db: Session,
    *,
    status: BatchStatus | None,
    source: ImportSource | None,
    limit: int,
    offset: int,
) -> tuple[Sequence[ImportBatch], int]:
    stmt = select(ImportBatch)
    if status is not None:
        stmt = stmt.where(ImportBatch.status == status)
    if source is not None:
        stmt = stmt.where(ImportBatch.source == source)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(ImportBatch.created_at.desc(), ImportBatch.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return rows, total


def get_batch(db: Session, batch_id: uuid.UUID) -> ImportBatch:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise NotFoundError("Import not found")
    return batch


def list_issues(
    db: Session,
    batch_id: uuid.UUID,
    *,
    outcome: IssueOutcome | None,
    limit: int,
    offset: int,
) -> tuple[Sequence[ImportRowIssue], int]:
    get_batch(db, batch_id)
    stmt = select(ImportRowIssue).where(ImportRowIssue.batch_id == batch_id)
    if outcome is not None:
        stmt = stmt.where(ImportRowIssue.outcome == outcome)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(ImportRowIssue.row_number, ImportRowIssue.id).limit(limit).offset(offset)
    ).all()
    return rows, total


def iter_issues(db: Session, batch_id: uuid.UUID) -> Iterator[ImportRowIssue]:
    """Checks the batch exists immediately (not on first iteration)."""
    get_batch(db, batch_id)
    return _iter_issues(db, batch_id)


def _iter_issues(db: Session, batch_id: uuid.UUID) -> Iterator[ImportRowIssue]:
    stmt = (
        select(ImportRowIssue)
        .where(ImportRowIssue.batch_id == batch_id)
        .order_by(ImportRowIssue.row_number, ImportRowIssue.id)
        .execution_options(yield_per=1000)
    )
    yield from db.scalars(stmt)
