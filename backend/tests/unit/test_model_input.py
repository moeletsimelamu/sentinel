"""Model-input vector: schema, explicit unknowns, reference statistics, new features."""

from __future__ import annotations

import math
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from sentinel.features import (
    FEATURE_SCHEMA_HASH,
    FORBIDDEN_INPUTS,
    MODEL_FEATURES,
    FeatureReference,
    build_feature_rows,
    to_model_vector,
)
from tests.unit.test_features import features_of, txn
from tests.unit.test_rules import QUIET

REF = FeatureReference(
    population_median_amount_zar=200.0, category_p95_amount_zar={"groceries": 1000.0}
)


def test_schema_is_stable_and_clean() -> None:
    assert len(MODEL_FEATURES) == len(set(MODEL_FEATURES))
    assert not set(MODEL_FEATURES) & FORBIDDEN_INPUTS
    assert len(FEATURE_SCHEMA_HASH) == 16
    vec = to_model_vector(QUIET, REF)
    assert list(vec) == list(MODEL_FEATURES)
    assert all(isinstance(v, float) and math.isfinite(v) for v in vec.values())


def test_unknowns_are_explicit() -> None:
    cold = replace(
        QUIET,
        history_count_90d=0,
        cust_median_amount_zar_90d=None,
        amount_vs_cust_median_90d=None,
        is_new_city_90d=None,
        is_new_device_90d=None,
        is_new_category_90d=None,
        hour_rarity_cust=None,
        channel_rarity_cust=None,
        category_share_shift_7d_vs_90d=None,
        km_from_prev_txn=None,
        implied_speed_kmh=None,
        velocity_ratio_24h_vs_30d=None,
        secs_since_prev_txn=None,
        history_span_days_90d=None,
    )
    vec = to_model_vector(cold, REF)
    assert vec["has_history"] == 0.0
    for name in ("is_new_city_90d", "hour_rarity_cust", "log_km_from_prev_txn"):
        assert vec[name] == -1.0
    # Cold start falls back to the population median from the training reference.
    assert vec["log_amount_vs_cust_median_90d"] == pytest.approx(math.log(100 / 200))
    assert vec["log_secs_since_prev_txn"] == pytest.approx(math.log1p(90 * 86400))


def test_encodings() -> None:
    vec = to_model_vector(
        replace(QUIET, hour_utc=6, day_of_week=6, is_new_device_90d=True, channel="atm"), REF
    )
    assert vec["hour_sin"] == pytest.approx(1.0) and vec["hour_cos"] == pytest.approx(0, abs=1e-12)
    assert vec["is_weekend"] == 1.0 and vec["is_new_device_90d"] == 1.0
    assert vec["channel_atm"] == 1.0 and vec["channel_online"] == 0.0
    assert vec["amount_vs_category_p95"] == pytest.approx(100 / 1000)


def test_outflow_vs_inflow_only_for_transfers_out() -> None:
    mule = replace(QUIET, txn_type="transfer_out", amount_zar=9000.0, inflow_sum_24h_zar=10000.0)
    assert to_model_vector(mule, REF)["outflow_vs_inflow_24h"] == pytest.approx(0.9)
    purchase = replace(mule, txn_type="purchase")
    assert to_model_vector(purchase, REF)["outflow_vs_inflow_24h"] == 0.0


def test_reference_fit_and_round_trip() -> None:
    rows = [replace(QUIET, amount_zar=float(a)) for a in range(1, 101)]
    ref = FeatureReference.fit(rows)
    assert ref.population_median_amount_zar == pytest.approx(50.5)
    assert ref.category_p95_amount_zar["groceries"] == pytest.approx(95.05)
    assert FeatureReference.from_dict(ref.to_dict()) == ref
    with pytest.raises(ValueError):
        FeatureReference.fit([])


# --------------------------------------------------------- behaviour-v2 fields


def test_spend_and_inflow_sums() -> None:
    history = [
        txn(-60, amount=200),
        txn(-50, amount=300, status="declined"),  # not completed
        replace(txn(-40, amount=5000), txn_type="transfer_in"),
        txn(-60 * 25, amount=999),  # outside 24h
    ]
    f = features_of(history, replace(txn(0, amount=4500), txn_type="transfer_out"))
    assert f.spend_sum_24h_zar == pytest.approx(200)
    assert f.inflow_sum_24h_zar == pytest.approx(5000)
    assert f.txn_type == "transfer_out"


def test_history_span_secs_since_prev_and_channel_rarity() -> None:
    history = [txn(-60 * 24 * 10), txn(-30)]
    f = features_of(history, txn(0))
    assert f.history_span_days_90d == pytest.approx(10)
    assert f.secs_since_prev_txn == pytest.approx(30 * 60)
    assert f.channel_rarity_cust == pytest.approx(0.0)  # all prior were "online"


def test_calendar_fields_use_utc_whatever_the_input_timezone() -> None:
    t = txn(0)
    sast = timezone(timedelta(hours=2))
    shifted = replace(t, occurred_at=t.occurred_at.astimezone(sast))
    a = build_feature_rows([t])[t.id]
    b = build_feature_rows([shifted])[t.id]
    assert a.hour_utc == b.hour_utc == 12 and a.day_of_week == b.day_of_week


def test_timezone_aware_inputs_are_required_semantics() -> None:
    t = txn(0)
    assert t.occurred_at.tzinfo is UTC
    assert build_feature_rows([t])[t.id].day_of_week == datetime(2026, 3, 1, tzinfo=UTC).weekday()


def test_ids_unchanged_by_vector() -> None:
    assert to_model_vector(replace(QUIET, transaction_id=uuid.uuid4()), REF) == to_model_vector(
        QUIET, REF
    )
