"""Plain-language descriptions of model inputs for analyst-facing reasons.

Reasons describe what was unusual about the transaction; they never say "fraud".
"""

from __future__ import annotations

import math

LABELS: dict[str, str] = {
    "log_amount_zar": "transaction amount",
    "log_amount_vs_cust_median_90d": "amount compared with this customer's usual amount",
    "amount_vs_category_p95": "amount compared with typical amounts in this category",
    "has_history": "whether the customer has recent history",
    "log_history_count_90d": "number of the customer's transactions in 90 days",
    "history_span_days_90d": "how long the customer has been active (90-day window)",
    "txn_count_1h": "transactions in the previous hour",
    "txn_count_24h": "transactions in the previous 24 hours",
    "log_velocity_ratio_24h_vs_30d": "24-hour activity compared with the 30-day average",
    "failed_count_24h": "declined or failed attempts in 24 hours",
    "same_category_count_10m": "repeat transactions in this category within 10 minutes",
    "log_spend_sum_24h_zar": "spending in the previous 24 hours",
    "log_inflow_sum_24h_zar": "money received in the previous 24 hours",
    "outflow_vs_inflow_24h": "outgoing transfer compared with money just received",
    "log_secs_since_prev_txn": "time since the previous transaction",
    "hour_sin": "time of day",
    "hour_cos": "time of day",
    "is_weekend": "weekend timing",
    "hour_rarity_cust": "how unusual this hour is for the customer",
    "channel_rarity_cust": "how unusual this channel is for the customer",
    "category_share_shift_7d_vs_90d": "change in the customer's spending mix",
    "log_km_from_prev_txn": "distance from the previous transaction",
    "log_implied_speed_kmh": "travel speed implied since the previous transaction",
    "is_new_city_90d": "city not seen for this customer in 90 days",
    "is_new_device_90d": "device not seen for this customer in 90 days",
    "is_new_category_90d": "merchant category not used by the customer in 90 days",
    "high_risk_category": "high-risk merchant category",
    "is_transfer_out": "outgoing transfer",
    "channel_card_present": "card-present channel",
    "channel_online": "online channel",
    "channel_mobile_app": "mobile app channel",
    "channel_atm": "ATM channel",
    "channel_bank_transfer": "bank transfer channel",
}


def label(feature: str) -> str:
    return LABELS.get(feature, feature.replace("_", " "))


def model_reason(feature: str, contribution: float) -> str:
    direction = "raised" if contribution > 0 else "lowered"
    if not math.isfinite(contribution):  # pragma: no cover - defensive
        return f"Model: {label(feature)}"
    return f"Model: {label(feature)} {direction} the model score ({contribution:+.3f})"
