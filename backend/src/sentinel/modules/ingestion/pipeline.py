"""Validate → de-duplicate → store, shared by CSV and REST ingestion.

Duplicate handling, in order:

1. Same ``external_id`` twice in one submission → later rows are duplicates.
2. ``(source_system, external_id)`` already stored → duplicate, not inserted (this is what
   makes re-importing the same file idempotent).
3. Same content hash as a stored transaction or an earlier row, but a different id →
   inserted, and reported as a *possible duplicate* for a person to review.

The unique constraint is still the final guard: if a concurrent import inserts the same
id first, ``ON CONFLICT DO NOTHING`` skips the row and it is counted as a duplicate.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session

from sentinel.core.config import Settings
from sentinel.core.ids import uuid7
from sentinel.core.security import hash_identifier
from sentinel.modules.ingestion.models import ImportBatch, ImportRowIssue, IssueOutcome
from sentinel.modules.ingestion.validation import (
    FieldError,
    ValidRecord,
    redact_raw_row,
    validate_record,
)
from sentinel.modules.transactions import service as transactions

MAX_EXTERNAL_ID_LENGTH = 64


@dataclass(frozen=True)
class IncomingRow:
    row_number: int
    values: Mapping[str, Any]
    # Problems found before field validation (wrong cell count, forbidden fields).
    pre_errors: tuple[FieldError, ...] = ()


@dataclass
class IssueDraft:
    row_number: int
    outcome: IssueOutcome
    external_id: str | None
    reasons: list[dict[str, Any]]
    raw_row: dict[str, Any] | None = None
    transaction_id: uuid.UUID | None = None


@dataclass
class IngestOutcome:
    row_count: int = 0
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    possible_duplicates: int = 0
    issues: list[IssueDraft] = field(default_factory=list)


def _reason(code: str, message: str, field_name: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"field": field_name, "code": code, "message": message, **extra}


def _reported_external_id(values: Mapping[str, Any], errors: Sequence[FieldError]) -> str | None:
    """The row's id for the report — omitted if it was itself invalid or sensitive."""
    if any(e.field == "external_id" for e in errors):
        return None
    value = values.get("external_id")
    if isinstance(value, str) and 0 < len(value.strip()) <= MAX_EXTERNAL_ID_LENGTH:
        return value.strip()
    return None


def ingest_rows(
    db: Session,
    batch: ImportBatch,
    rows: Sequence[IncomingRow],
    *,
    settings: Settings,
    now: datetime,
) -> IngestOutcome:
    """Validate and store ``rows`` for ``batch``. Updates the batch counters in place.

    Runs inside the caller's transaction; nothing is committed here.
    """
    out = IngestOutcome(row_count=len(rows))
    hash_device = partial(hash_identifier, settings)

    # 1. validate, and drop repeated ids within this submission
    valid: list[tuple[int, ValidRecord]] = []
    first_line_for_id: dict[str, int] = {}
    for row in rows:
        if row.pre_errors:
            errors: tuple[FieldError, ...] = row.pre_errors
            sensitive: frozenset[str] = frozenset()
        else:
            result = validate_record(row.values, now=now, hash_device=hash_device)
            if result.record is not None:
                rec = result.record
                if rec.external_id in first_line_for_id:
                    first = first_line_for_id[rec.external_id]
                    out.issues.append(
                        IssueDraft(
                            row.row_number,
                            IssueOutcome.DUPLICATE,
                            rec.external_id,
                            [_reason("duplicate_in_file", f"Same external_id as row {first}")],
                        )
                    )
                else:
                    first_line_for_id[rec.external_id] = row.row_number
                    valid.append((row.row_number, rec))
                continue
            errors, sensitive = result.errors, result.sensitive_fields
        out.issues.append(
            IssueDraft(
                row.row_number,
                IssueOutcome.REJECTED,
                _reported_external_id(row.values, errors),
                [e.as_dict() for e in errors],
                raw_row=redact_raw_row(row.values, sensitive),
            )
        )

    # 2. ids already stored for this source system
    already = transactions.existing_external_ids(
        db, batch.source_system, (rec.external_id for _, rec in valid)
    )
    fresh: list[tuple[int, ValidRecord]] = []
    for row_number, rec in valid:
        if rec.external_id in already:
            out.issues.append(_already_ingested(row_number, rec.external_id))
        else:
            fresh.append((row_number, rec))

    # 3. possible duplicates by content, then insert
    stored_hashes = transactions.existing_content_hashes(db, (r.content_hash for _, r in fresh))
    customer_ids = transactions.ensure_customers(db, (r.customer_ref for _, r in fresh))
    seen_hashes: dict[str, int] = {}
    pending: list[tuple[int, ValidRecord, uuid.UUID, dict[str, Any] | None]] = []
    for row_number, rec in fresh:
        txn_id = uuid7()
        similar: dict[str, Any] | None = None
        if rec.content_hash in stored_hashes:
            similar = _reason(
                "same_content_as_existing",
                "Same details as an existing transaction under a different external_id",
                related_transaction_id=str(stored_hashes[rec.content_hash]),
            )
        elif rec.content_hash in seen_hashes:
            similar = _reason(
                "same_content_in_file",
                f"Same details as row {seen_hashes[rec.content_hash]} "
                "under a different external_id",
            )
        seen_hashes.setdefault(rec.content_hash, row_number)
        pending.append((row_number, rec, txn_id, similar))

    inserted = transactions.insert_transactions(
        db,
        [
            {
                "id": txn_id,
                "external_id": rec.external_id,
                "source_system": batch.source_system,
                "customer_id": customer_ids[rec.customer_ref],
                "amount": rec.amount,
                "currency": rec.currency,
                "occurred_at": rec.occurred_at,
                "txn_type": rec.txn_type,
                "channel": rec.channel,
                "merchant_category": rec.merchant_category,
                "status": rec.status,
                "city": rec.city,
                "country": rec.country,
                "lat": rec.lat,
                "lon": rec.lon,
                "device_id_hash": rec.device_id_hash,
                "batch_id": batch.id,
                "content_hash": rec.content_hash,
                "ingested_at": now,
            }
            for _, rec, txn_id, _ in pending
        ],
    )
    for row_number, rec, txn_id, similar in pending:
        if rec.external_id not in inserted:  # lost a race with a concurrent import
            out.issues.append(_already_ingested(row_number, rec.external_id))
            continue
        out.accepted += 1
        if similar is not None:
            out.issues.append(
                IssueDraft(
                    row_number,
                    IssueOutcome.POSSIBLE_DUPLICATE,
                    rec.external_id,
                    [similar],
                    transaction_id=txn_id,
                )
            )

    out.issues.sort(key=lambda i: i.row_number)
    for issue in out.issues:
        if issue.outcome is IssueOutcome.REJECTED:
            out.rejected += 1
        elif issue.outcome is IssueOutcome.DUPLICATE:
            out.duplicates += 1
        else:
            out.possible_duplicates += 1
    _store_issues(db, batch.id, out.issues)

    batch.row_count = out.row_count
    batch.accepted_count = out.accepted
    batch.rejected_count = out.rejected
    batch.duplicate_count = out.duplicates
    batch.possible_duplicate_count = out.possible_duplicates
    return out


def _already_ingested(row_number: int, external_id: str) -> IssueDraft:
    return IssueDraft(
        row_number,
        IssueOutcome.DUPLICATE,
        external_id,
        [_reason("already_ingested", "A transaction with this external_id was already imported")],
    )


def _store_issues(db: Session, batch_id: uuid.UUID, issues: Sequence[IssueDraft]) -> None:
    rows = [
        {
            "batch_id": batch_id,
            "row_number": i.row_number,
            "outcome": i.outcome.value,
            "external_id": i.external_id,
            "transaction_id": i.transaction_id,
            "reasons": i.reasons,
            "raw_row": i.raw_row,
        }
        for i in issues
    ]
    for start in range(0, len(rows), transactions.CHUNK_SIZE):
        db.execute(insert(ImportRowIssue), rows[start : start + transactions.CHUNK_SIZE])
