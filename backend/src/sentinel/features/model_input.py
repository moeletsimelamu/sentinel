"""Turn a ``FeatureRow`` into the fixed numeric vector models consume.

The vector layout is identified by ``FEATURE_SCHEMA_HASH``; model manifests (Phase 6)
record it and the loader refuses a model whose hash differs.

Unknowns are encoded explicitly rather than imputed silently: boolean "is new" flags
become 1 / 0 / -1 (unknown), ratios with no history become -1, and ``has_history`` lets a
model learn how far to trust behavioural ratios. Population fallbacks come from a
``FeatureReference`` fitted on the **training split only**.

Deliberately absent: customer identifiers, the generator's segment, any label-derived
value, and protected attributes (none exist in the data).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sentinel.core.vocabulary import Channel
from sentinel.features.behaviour import FeatureRow

UNKNOWN = -1.0
SECONDS_90D = 90 * 86400

MODEL_FEATURES: tuple[str, ...] = (
    "log_amount_zar",
    "log_amount_vs_cust_median_90d",
    "amount_vs_category_p95",
    "has_history",
    "log_history_count_90d",
    "history_span_days_90d",
    "txn_count_1h",
    "txn_count_24h",
    "log_velocity_ratio_24h_vs_30d",
    "failed_count_24h",
    "same_category_count_10m",
    "log_spend_sum_24h_zar",
    "log_inflow_sum_24h_zar",
    "outflow_vs_inflow_24h",
    "log_secs_since_prev_txn",
    "hour_sin",
    "hour_cos",
    "is_weekend",
    "hour_rarity_cust",
    "channel_rarity_cust",
    "category_share_shift_7d_vs_90d",
    "log_km_from_prev_txn",
    "log_implied_speed_kmh",
    "is_new_city_90d",
    "is_new_device_90d",
    "is_new_category_90d",
    "high_risk_category",
    "is_transfer_out",
    *(f"channel_{c.value}" for c in Channel),
)

FEATURE_SCHEMA_HASH = hashlib.sha256("\n".join(MODEL_FEATURES).encode()).hexdigest()[:16]

# Inputs that must never reach a model (checked by the feature-schema test).
FORBIDDEN_INPUTS = frozenset(
    {
        "is_fraud",
        "is_fraud_injected",
        "is_fraud_labelled",
        "typology",
        "scenario_id",
        "label",
        "segment",
        "customer_ref",
        "customer_id",
        "external_id",
        "transaction_id",
    }
)


@dataclass(frozen=True)
class FeatureReference:
    """Population statistics from the training split (stored in the model manifest)."""

    population_median_amount_zar: float
    category_p95_amount_zar: Mapping[str, float] = field(default_factory=dict)

    @classmethod
    def fit(cls, rows: Iterable[FeatureRow]) -> FeatureReference:
        amounts: list[float] = []
        by_category: dict[str, list[float]] = {}
        for r in rows:
            amounts.append(r.amount_zar)
            by_category.setdefault(r.merchant_category, []).append(r.amount_zar)
        if not amounts:
            raise ValueError("Cannot fit a feature reference on no rows")
        return cls(
            population_median_amount_zar=_quantile(amounts, 0.5),
            category_p95_amount_zar={c: _quantile(v, 0.95) for c, v in sorted(by_category.items())},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "population_median_amount_zar": self.population_median_amount_zar,
            "category_p95_amount_zar": dict(self.category_p95_amount_zar),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FeatureReference:
        return cls(
            population_median_amount_zar=float(data["population_median_amount_zar"]),
            category_p95_amount_zar={
                str(k): float(v) for k, v in data["category_p95_amount_zar"].items()
            },
        )


def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def _flag(value: bool | None) -> float:
    return UNKNOWN if value is None else float(value)


def _log1p(value: float | None) -> float:
    return UNKNOWN if value is None else math.log1p(max(value, 0.0))


def to_model_vector(row: FeatureRow, ref: FeatureReference) -> dict[str, float]:
    """Numeric model inputs, keyed and ordered exactly as ``MODEL_FEATURES``."""
    median = row.cust_median_amount_zar_90d or ref.population_median_amount_zar
    category_p95 = ref.category_p95_amount_zar.get(
        row.merchant_category, ref.population_median_amount_zar
    )
    angle = 2 * math.pi * row.hour_utc / 24
    inflow = row.inflow_sum_24h_zar
    values: dict[str, float] = {
        "log_amount_zar": math.log1p(row.amount_zar),
        "log_amount_vs_cust_median_90d": math.log(max(row.amount_zar, 0.01) / max(median, 0.01)),
        "amount_vs_category_p95": row.amount_zar / max(category_p95, 0.01),
        "has_history": float(row.history_count_90d > 0),
        "log_history_count_90d": math.log1p(row.history_count_90d),
        "history_span_days_90d": row.history_span_days_90d or 0.0,
        "txn_count_1h": float(row.txn_count_1h),
        "txn_count_24h": float(row.txn_count_24h),
        "log_velocity_ratio_24h_vs_30d": _log1p(row.velocity_ratio_24h_vs_30d),
        "failed_count_24h": float(row.failed_count_24h),
        "same_category_count_10m": float(row.same_category_count_10m),
        "log_spend_sum_24h_zar": math.log1p(row.spend_sum_24h_zar),
        "log_inflow_sum_24h_zar": math.log1p(inflow),
        "outflow_vs_inflow_24h": (
            row.amount_zar / inflow if inflow > 0 and row.txn_type == "transfer_out" else 0.0
        ),
        "log_secs_since_prev_txn": math.log1p(
            row.secs_since_prev_txn if row.secs_since_prev_txn is not None else SECONDS_90D
        ),
        "hour_sin": math.sin(angle),
        "hour_cos": math.cos(angle),
        "is_weekend": float(row.day_of_week >= 5),
        "hour_rarity_cust": UNKNOWN if row.hour_rarity_cust is None else row.hour_rarity_cust,
        "channel_rarity_cust": (
            UNKNOWN if row.channel_rarity_cust is None else row.channel_rarity_cust
        ),
        "category_share_shift_7d_vs_90d": (
            UNKNOWN
            if row.category_share_shift_7d_vs_90d is None
            else row.category_share_shift_7d_vs_90d
        ),
        "log_km_from_prev_txn": _log1p(row.km_from_prev_txn),
        "log_implied_speed_kmh": _log1p(row.implied_speed_kmh),
        "is_new_city_90d": _flag(row.is_new_city_90d),
        "is_new_device_90d": _flag(row.is_new_device_90d),
        "is_new_category_90d": _flag(row.is_new_category_90d),
        "high_risk_category": float(row.high_risk_category),
        "is_transfer_out": float(row.txn_type == "transfer_out"),
    }
    for c in Channel:
        values[f"channel_{c.value}"] = float(row.channel == c.value)
    return {name: values[name] for name in MODEL_FEATURES}
