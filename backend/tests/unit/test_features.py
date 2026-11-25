"""Behavioural features: window boundaries, unknown-vs-zero, and no future leakage."""

from __future__ import annotations

import random
import uuid
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sentinel.features.behaviour import (
    FeatureRow,
    TxnRecord,
    build_feature_rows,
    haversine_km,
    js_distance,
)

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CUST = uuid.UUID(int=1)
JHB = (-26.2041, 28.0473)
CPT = (-33.9249, 18.4241)
LONDON = (51.5074, -0.1278)


def txn(
    minutes: float,
    *,
    amount: float = 100.0,
    currency: str = "ZAR",
    status: str = "completed",
    category: str = "groceries",
    city: str | None = "Johannesburg",
    loc: tuple[float, float] | None = JHB,
    device: str | None = "dev-1",
    customer: uuid.UUID = CUST,
) -> TxnRecord:
    return TxnRecord(
        id=uuid.uuid4(),
        customer_id=customer,
        occurred_at=T0 + timedelta(minutes=minutes),
        amount=amount,
        currency=currency,
        status=status,
        merchant_category=category,
        channel="online",
        city=city,
        lat=loc[0] if loc else None,
        lon=loc[1] if loc else None,
        device_id_hash=device,
    )


def features_of(history: list[TxnRecord], target: TxnRecord) -> FeatureRow:
    return build_feature_rows([*history, target], {target.id})[target.id]


def test_first_transaction_has_no_history_and_unknown_flags() -> None:
    f = features_of([], txn(0))
    assert f.history_count_90d == 0 and f.txn_count_1h == 0
    assert f.cust_median_amount_zar_90d is None and f.amount_vs_cust_median_90d is None
    assert f.is_new_city_90d is None and f.is_new_device_90d is None
    assert f.is_new_category_90d is None and f.hour_rarity_cust is None
    assert f.km_from_prev_txn is None and f.velocity_ratio_24h_vs_30d is None


def test_window_boundaries_are_half_open() -> None:
    target = txn(0)
    exactly_1h = txn(-60)  # occurred_at == T - 1h → inside [T-1h, T)
    just_over = txn(-60.01)
    f = features_of([exactly_1h, just_over], target)
    assert f.txn_count_1h == 1
    assert f.txn_count_24h == 2

    ten_min = txn(-10)
    over_ten = txn(-10.01)
    f = features_of([ten_min, over_ten], target)
    assert f.same_category_count_10m == 1


def test_same_instant_transactions_do_not_see_each_other() -> None:
    a, b = txn(0), txn(0)
    rows = build_feature_rows([a, b])
    assert rows[a.id].txn_count_1h == 0 and rows[b.id].txn_count_1h == 0


def test_90_day_window_and_median_uses_completed_only() -> None:
    history = [
        txn(-60 * 24 * 91, amount=1_000),  # outside 90 days
        txn(-60, amount=100),
        txn(-50, amount=300),
        txn(-40, amount=9_999, status="declined"),  # not part of the median
        txn(-30, amount=200),
    ]
    f = features_of(history, txn(0, amount=1_000))
    assert f.history_count_90d == 4
    assert f.cust_median_amount_zar_90d == 200
    assert f.amount_vs_cust_median_90d == pytest.approx(5.0)


def test_amounts_are_converted_to_zar() -> None:
    f = features_of([txn(-5, amount=100, currency="ZAR")], txn(0, amount=100, currency="USD"))
    assert f.amount_zar == pytest.approx(1800)
    assert f.amount_vs_cust_median_90d == pytest.approx(18)


def test_failed_attempts_in_24h() -> None:
    history = [
        txn(-60 * 25, status="declined"),  # outside 24h
        txn(-30, status="declined"),
        txn(-20, status="failed"),
        txn(-10, status="completed"),
    ]
    assert features_of(history, txn(0)).failed_count_24h == 2


def test_velocity_ratio() -> None:
    history = [txn(-60 * 24 * d) for d in range(2, 22)]  # one per day, days 2-21 ago
    history += [txn(-m) for m in (5, 10, 15, 20, 25)]  # burst today
    f = features_of(history, txn(0))
    assert f.txn_count_24h == 5 and f.txn_count_30d == 25
    assert f.velocity_ratio_24h_vs_30d == pytest.approx(5 / (25 / 30))


def test_distance_and_speed_from_previous_located_transaction() -> None:
    history = [txn(-120, loc=JHB), txn(-60, loc=None, city=None)]  # latest has no location
    f = features_of(history, txn(0, loc=CPT, city="Cape Town"))
    expected = haversine_km(*JHB, *CPT)
    assert f.km_from_prev_txn == pytest.approx(expected)
    assert f.implied_speed_kmh == pytest.approx(expected / 2)


def test_speed_is_bounded_for_near_simultaneous_events() -> None:
    f = features_of([txn(-0.001, loc=JHB)], txn(0, loc=LONDON))
    assert f.implied_speed_kmh is not None and f.implied_speed_kmh <= 20_000


def test_new_city_device_and_category_flags() -> None:
    history = [txn(-100, city="Johannesburg", device="dev-1", category="groceries")]
    f = features_of(history, txn(0, city="durban", device="dev-2", category="gift_cards"))
    assert f.is_new_city_90d is True and f.is_new_device_90d is True
    assert f.is_new_category_90d is True and f.high_risk_category is True
    same = features_of(history, txn(0, city="JOHANNESBURG", device="dev-1"))
    assert same.is_new_city_90d is False and same.is_new_device_90d is False
    missing = features_of(history, txn(0, city=None, device=None))
    assert missing.is_new_city_90d is None and missing.is_new_device_90d is None


def test_hour_rarity() -> None:
    # Ten daytime transactions at 12:00 UTC; one at 13:00 is "near"; 03:00 is never seen.
    history = [txn(-60 * 24 * d) for d in range(1, 11)]
    assert features_of(history, txn(60)).hour_rarity_cust == pytest.approx(0.0)
    assert features_of(history, txn(-9 * 60)).hour_rarity_cust == pytest.approx(1.0)


def test_category_shift_distance() -> None:
    history = [txn(-60 * 24 * d, category="groceries") for d in range(8, 40)]
    stable = features_of(history, txn(0, category="groceries"))
    assert stable.category_share_shift_7d_vs_90d == pytest.approx(0.0, abs=1e-9)
    shifted = features_of(history, txn(0, category="crypto_exchange"))
    assert shifted.category_share_shift_7d_vs_90d == pytest.approx(1.0)


def test_js_distance_properties() -> None:
    a, b = Counter({"x": 3, "y": 1}), Counter({"x": 1, "y": 3})
    assert js_distance(a, a) == pytest.approx(0)
    assert js_distance(a, b) == pytest.approx(js_distance(b, a))
    assert 0 < js_distance(a, b) < 1
    assert js_distance(Counter({"x": 1}), Counter({"y": 1})) == pytest.approx(1)


def test_customers_are_isolated() -> None:
    other = uuid.UUID(int=2)
    f = features_of([txn(-5, customer=other) for _ in range(10)], txn(0))
    assert f.txn_count_1h == 0 and f.history_count_90d == 0


def test_no_future_leakage() -> None:
    """Adding transactions after T (or at T) must never change features at T."""
    rng = random.Random(7)
    cities = [("Johannesburg", JHB), ("Cape Town", CPT), ("London", LONDON)]
    history = []
    for _ in range(300):
        city, loc = rng.choice(cities)
        history.append(
            txn(
                rng.uniform(-60 * 24 * 100, 0),
                amount=rng.uniform(5, 5000),
                status=rng.choice(["completed", "completed", "declined"]),
                category=rng.choice(["groceries", "fuel", "gift_cards", "electronics"]),
                city=city,
                loc=loc,
                device=rng.choice(["d1", "d2", None]),
            )
        )
    cutoff = T0 - timedelta(days=30)
    past = [t for t in history if t.occurred_at < cutoff]
    targets = {t.id for t in past}
    base = build_feature_rows(past, targets)
    # Future events, including some at exactly the latest past instant
    latest = max(t.occurred_at for t in past)
    future = [t for t in history if t.occurred_at >= cutoff]
    future += [replace(past[0], id=uuid.uuid4(), occurred_at=latest)]
    with_future = build_feature_rows(past + future, targets)
    assert base == with_future


def test_input_order_does_not_matter() -> None:
    history = [txn(-m, amount=float(m)) for m in range(1, 50)]
    shuffled = history[::-1]
    assert build_feature_rows(history) == build_feature_rows(shuffled)
