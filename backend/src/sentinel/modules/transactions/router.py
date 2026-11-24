from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from sentinel.core.db import get_db
from sentinel.core.permissions import Permission
from sentinel.core.vocabulary import Channel, MerchantCategory, TxnStatus, TxnType
from sentinel.modules.auth.dependencies import RequirePermission
from sentinel.modules.transactions import service
from sentinel.modules.transactions.schemas import TransactionOut, TransactionPage

router = APIRouter(prefix="/transactions", tags=["transactions"])

_read = RequirePermission(Permission.TRANSACTIONS_READ)


@router.get("", response_model=TransactionPage, dependencies=[Depends(_read)])
def list_transactions(
    customer_ref: str | None = Query(default=None, max_length=64),
    batch_id: uuid.UUID | None = None,
    status: TxnStatus | None = None,
    txn_type: TxnType | None = None,
    channel: Channel | None = None,
    merchant_category: MerchantCategory | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    min_amount: Decimal | None = Query(default=None, ge=0),
    max_amount: Decimal | None = Query(default=None, ge=0),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> TransactionPage:
    filters = service.TransactionFilters(
        customer_ref=customer_ref,
        batch_id=batch_id,
        status=status,
        txn_type=txn_type,
        channel=channel,
        merchant_category=merchant_category,
        occurred_from=occurred_from,
        occurred_to=occurred_to,
        min_amount=min_amount,
        max_amount=max_amount,
    )
    rows, next_cursor = service.list_transactions(db, filters, cursor=cursor, limit=limit)
    return TransactionPage(
        items=[TransactionOut.from_model(t) for t in rows], next_cursor=next_cursor
    )


@router.get("/{transaction_id}", response_model=TransactionOut, dependencies=[Depends(_read)])
def get_transaction(transaction_id: uuid.UUID, db: Session = Depends(get_db)) -> TransactionOut:
    return TransactionOut.from_model(service.get_transaction(db, transaction_id))
