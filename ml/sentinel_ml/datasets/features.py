"""Offline feature extraction using the *same* code as the scoring worker.

``sentinel.features.build_feature_rows`` is imported from the backend package — there
is no second implementation to drift. Features are computed per customer to bound
memory, and labels are never passed in: the feature table is built from ingestible
columns only and joined to labels by ``external_id`` at training time.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import asdict, fields

import pandas as pd
from sentinel.features import (
    FeatureReference,
    FeatureRow,
    TxnRecord,
    build_feature_rows,
    to_model_vector,
)

ID_NAMESPACE = uuid.UUID("8a0f3c52-2d4e-4f1b-9c55-5e47a1f3b001")
FEATURE_COLUMNS = tuple(f.name for f in fields(FeatureRow) if f.name != "transaction_id")


def transaction_uuid(external_id: str) -> uuid.UUID:
    return uuid.uuid5(ID_NAMESPACE, external_id)


def _records(df: pd.DataFrame) -> Iterator[TxnRecord]:
    def opt(value: object) -> object:
        return None if pd.isna(value) else value

    for row in df.itertuples(index=False):
        yield TxnRecord(
            id=transaction_uuid(row.external_id),
            customer_id=uuid.uuid5(ID_NAMESPACE, row.customer_ref),
            occurred_at=row.occurred_at.to_pydatetime(),
            amount=float(row.amount),
            currency=row.currency,
            status=row.status,
            merchant_category=row.merchant_category,
            channel=row.channel,
            city=opt(row.city),
            lat=opt(row.lat),
            lon=opt(row.lon),
            device_id_hash=opt(row.device_id),
            txn_type=row.txn_type,
        )


def feature_rows(transactions: pd.DataFrame) -> Iterator[tuple[str, FeatureRow]]:
    """Yield ``(external_id, FeatureRow)`` for every transaction, customer by customer."""
    for _, group in transactions.groupby("customer_ref", sort=True):
        ids = {transaction_uuid(e): e for e in group["external_id"]}
        rows = build_feature_rows(_records(group))
        for tid, ext in ids.items():
            yield ext, rows[tid]


def build_feature_table(transactions: pd.DataFrame) -> pd.DataFrame:
    """One row per transaction: ``external_id`` + every ``FeatureRow`` field."""
    cols: dict[str, list[object]] = {"external_id": [], **{c: [] for c in FEATURE_COLUMNS}}
    for ext, row in feature_rows(transactions):
        cols["external_id"].append(ext)
        data = asdict(row)
        for c in FEATURE_COLUMNS:
            cols[c].append(data[c])
    table = pd.DataFrame(cols)
    order = transactions[["external_id"]].reset_index(drop=True)
    return order.merge(table, on="external_id", how="left", validate="one_to_one")


def row_from_table(record: dict[str, object]) -> FeatureRow:
    values = {c: (None if _missing(record[c]) else record[c]) for c in FEATURE_COLUMNS}
    return FeatureRow(transaction_id=uuid.UUID(int=0), **values)


def _missing(value: object) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def fit_reference(features: pd.DataFrame) -> FeatureReference:
    """Population statistics — call with the **training split only**."""
    return FeatureReference.fit(row_from_table(r) for r in features.to_dict(orient="records"))


def model_matrix(features: pd.DataFrame, reference: FeatureReference) -> pd.DataFrame:
    """The numeric model inputs (``MODEL_FEATURES`` columns), indexed by ``external_id``."""
    vectors = [
        to_model_vector(row_from_table(r), reference) for r in features.to_dict(orient="records")
    ]
    return pd.DataFrame(vectors, index=features["external_id"].to_numpy())
