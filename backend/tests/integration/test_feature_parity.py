"""Train/serve parity.

Training computes features offline over each customer's complete history; the worker
computes them from the database, loading only the 90 days before each batch. Both must
produce *identical* values. The history spans 130 days and arrives in two chronological
imports, so the second import's features depend on history from the first.
"""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.core.permissions import Role
from sentinel.features import TxnRecord, build_feature_rows
from sentinel.modules.scoring.models import RiskAssessment
from sentinel.modules.transactions.models import Transaction
from tests.conftest import AuthHeaders
from tests.integration.test_ingestion import run_worker

pytestmark = pytest.mark.integration

START = datetime(2026, 2, 1, tzinfo=UTC)
CITIES = [("Johannesburg", "ZA", -26.2041, 28.0473), ("Cape Town", "ZA", -33.9249, 18.4241),
          ("London", "GB", 51.5074, -0.1278)]  # fmt: skip
CATEGORIES = ["groceries", "fuel", "restaurants", "gift_cards", "electronics", "money_transfer"]


def make_records(seed: int = 11) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    records = []
    n = 0
    for c in range(3):
        for day in range(130):
            for _ in range(rng.choice([0, 0, 1, 1, 2, 4])):
                n += 1
                city = rng.choice(CITIES) if rng.random() < 0.8 else None
                category = rng.choice(CATEGORIES)
                ts = START + timedelta(days=day, minutes=rng.randint(0, 1439))
                records.append(
                    {
                        "external_id": f"PAR-{n:05d}",
                        "customer_ref": f"C-PAR-{c}",
                        "occurred_at": ts.isoformat().replace("+00:00", "Z"),
                        "amount": f"{rng.uniform(5, 20000):.2f}",
                        "currency": rng.choice(["ZAR", "ZAR", "ZAR", "USD"]),
                        "txn_type": (
                            rng.choice(["transfer_out", "transfer_in"])
                            if category == "money_transfer"
                            else "purchase"
                        ),
                        "channel": rng.choice(["online", "card_present", "mobile_app"]),
                        "merchant_category": category,
                        "status": rng.choices(["completed", "declined", "failed"], [90, 8, 2])[0],
                        "city": city[0] if city else None,
                        "country": city[1] if city else None,
                        "lat": city[2] if city else None,
                        "lon": city[3] if city else None,
                        "device_id": rng.choice(["dev-a", "dev-b", None]),
                    }
                )
    records.sort(key=lambda r: (r["occurred_at"], r["external_id"]))
    return records


def offline_record(r: dict[str, Any], txn: Transaction) -> TxnRecord:
    return TxnRecord(
        id=txn.id,
        customer_id=uuid.uuid5(uuid.NAMESPACE_URL, r["customer_ref"]),
        occurred_at=datetime.fromisoformat(r["occurred_at"]),
        amount=float(r["amount"]),
        currency=r["currency"],
        status=r["status"],
        merchant_category=r["merchant_category"],
        channel=r["channel"],
        city=r["city"],
        lat=r["lat"],
        lon=r["lon"],
        device_id_hash=r["device_id"],  # raw id offline; only equality matters
        txn_type=r["txn_type"],
    )


def test_worker_features_equal_offline_features(
    client: TestClient, auth_headers: AuthHeaders, db: Session
) -> None:
    records = make_records()
    cutoff = (START + timedelta(days=100)).isoformat().replace("+00:00", "Z")
    first = [r for r in records if r["occurred_at"] < cutoff]
    second = [r for r in records if r["occurred_at"] >= cutoff]
    assert len(first) > 300 and 50 < len(second) <= 1000
    headers = auth_headers(Role.ANALYST)
    for chunk_start in range(0, len(first), 1000):
        chunk = first[chunk_start : chunk_start + 1000]
        resp = client.post(
            "/api/v1/transactions",
            json={"source_system": "parity", "records": chunk},
            headers=headers,
        )
        assert resp.status_code == 200 and resp.json()["batch"]["rejected_count"] == 0
    run_worker(db)
    resp = client.post(
        "/api/v1/transactions", json={"source_system": "parity", "records": second}, headers=headers
    )
    assert resp.status_code == 200
    run_worker(db)

    stored = {
        t.external_id: t
        for t in db.scalars(select(Transaction).where(Transaction.source_system == "parity"))
    }
    assert len(stored) == len(records)
    offline = build_feature_rows(offline_record(r, stored[r["external_id"]]) for r in records)

    assessments = db.scalars(
        select(RiskAssessment).join(Transaction).where(Transaction.source_system == "parity")
    ).all()
    assert len(assessments) == len(records)
    mismatches = []
    for a in assessments:
        expected = offline[a.transaction_id].as_dict()
        if a.features != expected:
            diff = {
                k: (a.features.get(k), v) for k, v in expected.items() if a.features.get(k) != v
            }
            mismatches.append(diff)
    assert not mismatches, mismatches[:3]
    # The second import genuinely depended on history from the first one.
    late = [a for a in assessments if stored_ext(stored, a) >= second[0]["external_id"]]
    assert any(a.features["history_count_90d"] > 20 for a in late)


def stored_ext(stored: dict[str, Transaction], a: RiskAssessment) -> str:
    return next(ext for ext, t in stored.items() if t.id == a.transaction_id)
