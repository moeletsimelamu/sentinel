from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from sentinel.core.db import Base
from sentinel.core.ids import uuid7
from sentinel.core.vocabulary import CURRENCIES, Channel, MerchantCategory, TxnStatus, TxnType


def _one_of(column: str, values: Iterable[object]) -> str:
    quoted = ", ".join(f"'{v}'" for v in sorted(str(v) for v in values))
    return f"{column} IN ({quoted})"


class Customer(Base):
    """A pseudonymous synthetic customer.

    Deliberately holds no name, date of birth, gender, ethnicity, nationality or address.
    The generator's behavioural ``segment`` is not stored either: it is an artefact of how
    the data was made and would leak into features.
    """

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    external_ref: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Transaction(Base):
    """An accepted transaction. Immutable: UPDATE/DELETE/TRUNCATE are rejected by trigger."""

    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    external_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_system: Mapped[str] = mapped_column(String(32), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    txn_type: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    merchant_category: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    city: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(2))
    lat: Mapped[float | None] = mapped_column(Double)
    lon: Mapped[float | None] = mapped_column(Double)
    device_id_hash: Mapped[str | None] = mapped_column(String(64))
    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("import_batches.id", ondelete="RESTRICT"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    customer: Mapped[Customer] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("source_system", "external_id", name="uq_transactions_source_external_id"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(_one_of("currency", CURRENCIES), name="currency_valid"),
        CheckConstraint(_one_of("txn_type", TxnType), name="txn_type_valid"),
        CheckConstraint(_one_of("channel", Channel), name="channel_valid"),
        CheckConstraint(_one_of("merchant_category", MerchantCategory), name="category_valid"),
        CheckConstraint(_one_of("status", TxnStatus), name="status_valid"),
        CheckConstraint("lat IS NULL OR lat BETWEEN -90 AND 90", name="lat_range"),
        CheckConstraint("lon IS NULL OR lon BETWEEN -180 AND 180", name="lon_range"),
        CheckConstraint("(lat IS NULL) = (lon IS NULL)", name="coordinates_paired"),
        Index("ix_transactions_customer_time", "customer_id", "occurred_at"),
        Index("ix_transactions_time_id", "occurred_at", "id"),
        Index("ix_transactions_batch_id", "batch_id"),
        Index("ix_transactions_content_hash", "content_hash"),
    )
