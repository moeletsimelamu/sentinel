"""Transaction storage and read models.

Write helpers are bulk, set-based and safe under concurrent imports: inserts use
``ON CONFLICT DO NOTHING`` so the unique constraints, not application checks, are the
final word on duplicates.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from sentinel.core.errors import NotFoundError, ValidationFailedError
from sentinel.core.ids import uuid7
from sentinel.modules.transactions.models import Customer, Transaction

CHUNK_SIZE = 1000


def _chunks[T](items: Sequence[T], size: int = CHUNK_SIZE) -> Iterator[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ------------------------------------------------------------------- writes


def ensure_customers(db: Session, refs: Iterable[str]) -> dict[str, uuid.UUID]:
    """Return ``{external_ref: id}``, creating pseudonymous customers that do not exist.

    Refs are processed in sorted order so concurrent imports lock rows consistently.
    """
    ordered = sorted(set(refs))
    ids: dict[str, uuid.UUID] = {}
    for chunk in _chunks(ordered):
        db.execute(
            pg_insert(Customer)
            .values([{"id": uuid7(), "external_ref": r} for r in chunk])
            .on_conflict_do_nothing(index_elements=["external_ref"])
        )
        rows = db.execute(
            select(Customer.external_ref, Customer.id).where(Customer.external_ref.in_(chunk))
        )
        ids.update({ref: cid for ref, cid in rows})
    return ids


def existing_external_ids(db: Session, source_system: str, external_ids: Iterable[str]) -> set[str]:
    found: set[str] = set()
    for chunk in _chunks(sorted(set(external_ids))):
        found.update(
            db.scalars(
                select(Transaction.external_id).where(
                    Transaction.source_system == source_system,
                    Transaction.external_id.in_(chunk),
                )
            )
        )
    return found


def existing_content_hashes(db: Session, hashes: Iterable[str]) -> dict[str, uuid.UUID]:
    """Map content hash → an existing transaction with that content."""
    found: dict[str, uuid.UUID] = {}
    for chunk in _chunks(sorted(set(hashes))):
        rows = db.execute(
            select(Transaction.content_hash, Transaction.id).where(
                Transaction.content_hash.in_(chunk)
            )
        )
        for h, tid in rows:
            found.setdefault(h, tid)
    return found


def insert_transactions(db: Session, rows: Sequence[dict[str, Any]]) -> set[str]:
    """Insert rows; return the external ids actually inserted (conflicts are skipped)."""
    inserted: set[str] = set()
    for chunk in _chunks(rows):
        result = db.execute(
            pg_insert(Transaction)
            .values(list(chunk))
            .on_conflict_do_nothing(constraint="uq_transactions_source_external_id")
            .returning(Transaction.external_id)
        )
        inserted.update(result.scalars())
    return inserted


# -------------------------------------------------------------------- reads


@dataclass(frozen=True)
class TransactionFilters:
    customer_ref: str | None = None
    batch_id: uuid.UUID | None = None
    status: str | None = None
    txn_type: str | None = None
    channel: str | None = None
    merchant_category: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    min_amount: Decimal | None = None
    max_amount: Decimal | None = None


def encode_cursor(txn: Transaction) -> str:
    raw = f"{txn.occurred_at.isoformat()}|{txn.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        ts, tid = base64.urlsafe_b64decode(padded.encode()).decode().split("|", 1)
        return datetime.fromisoformat(ts), uuid.UUID(tid)
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise ValidationFailedError("Invalid cursor", code="invalid_cursor") from None


def list_transactions(
    db: Session, filters: TransactionFilters, *, cursor: str | None, limit: int
) -> tuple[list[Transaction], str | None]:
    """Newest first, keyset-paginated on ``(occurred_at, id)``."""
    stmt = select(Transaction).order_by(Transaction.occurred_at.desc(), Transaction.id.desc())
    f = filters
    if f.customer_ref:
        stmt = stmt.join(Customer).where(Customer.external_ref == f.customer_ref)
    if f.batch_id:
        stmt = stmt.where(Transaction.batch_id == f.batch_id)
    if f.status:
        stmt = stmt.where(Transaction.status == f.status)
    if f.txn_type:
        stmt = stmt.where(Transaction.txn_type == f.txn_type)
    if f.channel:
        stmt = stmt.where(Transaction.channel == f.channel)
    if f.merchant_category:
        stmt = stmt.where(Transaction.merchant_category == f.merchant_category)
    if f.occurred_from:
        stmt = stmt.where(Transaction.occurred_at >= f.occurred_from)
    if f.occurred_to:
        stmt = stmt.where(Transaction.occurred_at < f.occurred_to)
    if f.min_amount is not None:
        stmt = stmt.where(Transaction.amount >= f.min_amount)
    if f.max_amount is not None:
        stmt = stmt.where(Transaction.amount <= f.max_amount)
    if cursor:
        ts, tid = decode_cursor(cursor)
        stmt = stmt.where(tuple_(Transaction.occurred_at, Transaction.id) < tuple_(ts, tid))
    rows = list(db.scalars(stmt.limit(limit + 1)).unique())
    next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor


def get_transaction(db: Session, transaction_id: uuid.UUID) -> Transaction:
    txn = db.get(Transaction, transaction_id)
    if txn is None:
        raise NotFoundError("Transaction not found")
    return txn
