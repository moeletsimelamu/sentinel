from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from sentinel.modules.transactions.models import Transaction


class TransactionOut(BaseModel):
    id: uuid.UUID
    external_id: str
    source_system: str
    customer_ref: str
    amount: Decimal
    currency: str
    occurred_at: datetime
    txn_type: str
    channel: str
    merchant_category: str
    status: str
    city: str | None
    country: str | None
    lat: float | None
    lon: float | None
    has_device_id: bool
    batch_id: uuid.UUID
    ingested_at: datetime

    @classmethod
    def from_model(cls, txn: Transaction) -> TransactionOut:
        return cls(
            id=txn.id,
            external_id=txn.external_id,
            source_system=txn.source_system,
            customer_ref=txn.customer.external_ref,
            amount=txn.amount,
            currency=txn.currency,
            occurred_at=txn.occurred_at,
            txn_type=txn.txn_type,
            channel=txn.channel,
            merchant_category=txn.merchant_category,
            status=txn.status,
            city=txn.city,
            country=txn.country,
            lat=txn.lat,
            lon=txn.lon,
            has_device_id=txn.device_id_hash is not None,
            batch_id=txn.batch_id,
            ingested_at=txn.ingested_at,
        )


class TransactionPage(BaseModel):
    items: list[TransactionOut]
    next_cursor: str | None
