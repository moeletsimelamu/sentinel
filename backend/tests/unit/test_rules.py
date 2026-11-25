"""Every rule: hit, miss and boundary. Plus engine and risk combination."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

import pytest

from sentinel.detection.engine import (
    ConfiguredRule,
    InvalidRuleParamsError,
    RuleHit,
    UnknownRuleError,
    default_params,
    evaluate,
    validate_params,
)
from sentinel.detection.risk import Band, ScoringMode, ScoringSpec, band_for, combine
from sentinel.detection.rules import CATALOGUE, Severity
from sentinel.features.behaviour import FeatureRow

QUIET = FeatureRow(
    transaction_id=uuid.UUID(int=1),
    amount=100.0,
    currency="ZAR",
    amount_zar=100.0,
    hour_utc=12,
    day_of_week=2,
    merchant_category="groceries",
    channel="online",
    txn_type="purchase",
    high_risk_category=False,
    history_count_90d=50,
    history_span_days_90d=85.0,
    secs_since_prev_txn=3600.0,
    cust_median_amount_zar_90d=100.0,
    amount_vs_cust_median_90d=1.0,
    txn_count_1h=0,
    txn_count_24h=1,
    txn_count_30d=30,
    velocity_ratio_24h_vs_30d=1.0,
    failed_count_24h=0,
    same_category_count_10m=0,
    spend_sum_24h_zar=100.0,
    inflow_sum_24h_zar=0.0,
    km_from_prev_txn=1.0,
    implied_speed_kmh=10.0,
    is_new_city_90d=False,
    is_new_device_90d=False,
    is_new_category_90d=False,
    channel_rarity_cust=0.3,
    hour_rarity_cust=0.2,
    category_share_shift_7d_vs_90d=0.05,
)


def fires(code: str, **overrides: Any) -> bool:
    rule = CATALOGUE[code]
    return rule.check(replace(QUIET, **overrides), rule.params_schema()) is not None


def test_quiet_row_triggers_nothing() -> None:
    assert [c for c in CATALOGUE if fires(c)] == []


def test_catalogue_has_the_twelve_v1_rules() -> None:
    assert len(CATALOGUE) == 12
    for code, rule in CATALOGUE.items():
        assert rule.code == code and 0 <= rule.default_weight <= 100
        assert rule.params_schema().model_dump()  # defaults are valid


# (code, overrides that sit exactly on the threshold, overrides just below it)
CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("LARGE_AMOUNT_ABSOLUTE", {"amount_zar": 50_000.0}, {"amount_zar": 49_999.99}),
    (
        "LARGE_AMOUNT_RELATIVE",
        {"amount_vs_cust_median_90d": 8.0, "history_count_90d": 10},
        {"amount_vs_cust_median_90d": 7.99, "history_count_90d": 10},
    ),
    ("RAPID_REPEAT", {"same_category_count_10m": 2}, {"same_category_count_10m": 1}),
    ("VELOCITY_1H", {"txn_count_1h": 6}, {"txn_count_1h": 5}),
    (
        "VELOCITY_RATIO_24H",
        {"velocity_ratio_24h_vs_30d": 5.0, "txn_count_24h": 5},
        {"velocity_ratio_24h_vs_30d": 4.99, "txn_count_24h": 5},
    ),
    ("FAILED_ATTEMPTS", {"failed_count_24h": 3}, {"failed_count_24h": 2}),
    (
        "IMPOSSIBLE_TRAVEL",
        {"km_from_prev_txn": 300.0, "implied_speed_kmh": 900.0},
        {"km_from_prev_txn": 300.0, "implied_speed_kmh": 899.9},
    ),
    (
        "NEW_LOCATION_HIGH_VALUE",
        {"is_new_city_90d": True, "amount_vs_cust_median_90d": 3.0},
        {"is_new_city_90d": True, "amount_vs_cust_median_90d": 2.99},
    ),
    (
        "UNUSUAL_HOUR",
        {"hour_rarity_cust": 0.97, "amount_vs_cust_median_90d": 2.0},
        {"hour_rarity_cust": 0.969, "amount_vs_cust_median_90d": 2.0},
    ),
    (
        "NEW_DEVICE_HIGH_VALUE",
        {"is_new_device_90d": True, "amount_vs_cust_median_90d": 3.0},
        {"is_new_device_90d": True, "amount_vs_cust_median_90d": 2.99},
    ),
    (
        "HIGH_RISK_CATEGORY_SHIFT",
        {
            "high_risk_category": True,
            "is_new_category_90d": True,
            "merchant_category": "gift_cards",
        },
        {"high_risk_category": True, "is_new_category_90d": False},
    ),
    (
        "BEHAVIOUR_SHIFT",
        {"category_share_shift_7d_vs_90d": 0.5, "history_count_90d": 20},
        {"category_share_shift_7d_vs_90d": 0.49, "history_count_90d": 20},
    ),
]


def test_every_rule_has_a_case() -> None:
    assert {c for c, _, _ in CASES} == set(CATALOGUE)


@pytest.mark.parametrize(("code", "hit", "miss"), CASES, ids=[c for c, _, _ in CASES])
def test_rule_boundary(code: str, hit: dict[str, Any], miss: dict[str, Any]) -> None:
    assert fires(code, **hit), "threshold value must trigger"
    assert not fires(code, **miss), "just below threshold must not trigger"


# Rules that depend on history must stay silent when it is missing or too thin.
SPARSE: list[tuple[str, dict[str, Any]]] = [
    ("LARGE_AMOUNT_RELATIVE", {"amount_vs_cust_median_90d": 50.0, "history_count_90d": 9}),
    ("LARGE_AMOUNT_RELATIVE", {"amount_vs_cust_median_90d": None, "history_count_90d": 0}),
    ("VELOCITY_RATIO_24H", {"velocity_ratio_24h_vs_30d": None, "txn_count_24h": 9}),
    ("IMPOSSIBLE_TRAVEL", {"km_from_prev_txn": None, "implied_speed_kmh": None}),
    ("NEW_LOCATION_HIGH_VALUE", {"is_new_city_90d": None, "amount_vs_cust_median_90d": 9.0}),
    (
        "NEW_LOCATION_HIGH_VALUE",
        {"is_new_city_90d": True, "amount_vs_cust_median_90d": 9.0, "history_count_90d": 4},
    ),
    ("UNUSUAL_HOUR", {"hour_rarity_cust": None, "amount_vs_cust_median_90d": 9.0}),
    ("NEW_DEVICE_HIGH_VALUE", {"is_new_device_90d": None, "amount_vs_cust_median_90d": 9.0}),
    (
        "HIGH_RISK_CATEGORY_SHIFT",
        {"high_risk_category": True, "is_new_category_90d": None},
    ),
    ("BEHAVIOUR_SHIFT", {"category_share_shift_7d_vs_90d": 0.9, "history_count_90d": 19}),
]


@pytest.mark.parametrize(("code", "overrides"), SPARSE)
def test_rules_are_silent_without_enough_history(code: str, overrides: dict[str, Any]) -> None:
    assert not fires(code, **overrides)


def test_impossible_travel_needs_distance_too() -> None:
    assert not fires("IMPOSSIBLE_TRAVEL", km_from_prev_txn=299.0, implied_speed_kmh=5000.0)


def test_explanations_are_specific_and_never_accuse() -> None:
    rule = CATALOGUE["LARGE_AMOUNT_RELATIVE"]
    obs = rule.check(
        replace(QUIET, amount_zar=1120.0, amount_vs_cust_median_90d=11.2),
        rule.params_schema(),
    )
    assert obs is not None
    assert obs.explanation == (
        "Amount ZAR 1,120.00 is 11.2x this customer's 90-day median (threshold 8.0x)"
    )
    assert obs.observed == {"amount_vs_median": 11.2, "history_count_90d": 50}
    assert obs.threshold == {"min_multiple": 8.0, "min_history": 10}
    for code, hit, _ in CASES:
        r = CATALOGUE[code]
        o = r.check(replace(QUIET, **hit), r.params_schema())
        assert o is not None and "fraud" not in o.explanation.lower()


# ------------------------------------------------------------------ params


def test_params_validation() -> None:
    p = validate_params("VELOCITY_1H", {"min_prior_count": 9})
    assert p.model_dump() == {"min_prior_count": 9}
    with pytest.raises(InvalidRuleParamsError) as info:
        validate_params("VELOCITY_1H", {"min_prior_count": 0})
    assert info.value.errors[0]["loc"] == ["min_prior_count"]
    with pytest.raises(InvalidRuleParamsError):
        validate_params("VELOCITY_1H", {"surprise": 1})  # extra keys are refused
    with pytest.raises(InvalidRuleParamsError):
        validate_params("UNUSUAL_HOUR", {"min_rarity": 1.5})
    with pytest.raises(UnknownRuleError):
        validate_params("NOPE", {})


def test_default_params_round_trip() -> None:
    for code in CATALOGUE:
        assert validate_params(code, default_params(code)).model_dump() == default_params(code)


# ------------------------------------------------------------------ engine


def configured(code: str, **kw: Any) -> ConfiguredRule:
    rule = CATALOGUE[code]
    return ConfiguredRule(
        rule_version_id=uuid.uuid4(),
        code=code,
        params=kw.pop("params", rule.params_schema()),
        weight=kw.pop("weight", rule.default_weight),
        severity=kw.pop("severity", rule.default_severity),
        force_alert=kw.pop("force_alert", rule.default_force_alert),
        is_active=kw.pop("is_active", True),
    )


def test_engine_skips_inactive_rules_and_uses_configured_params() -> None:
    row = replace(QUIET, txn_count_1h=7, failed_count_24h=5)
    hits = evaluate(
        [
            configured("VELOCITY_1H"),
            configured("FAILED_ATTEMPTS", is_active=False),
            configured("RAPID_REPEAT"),
        ],
        row,
    )
    assert [h.code for h in hits] == ["VELOCITY_1H"]
    strict = configured(
        "VELOCITY_1H", params=validate_params("VELOCITY_1H", {"min_prior_count": 8})
    )
    assert evaluate([strict], row) == []


# -------------------------------------------------------------------- risk

SPEC = ScoringSpec(
    w_rule=0.5,
    w_model=0.5,
    rule_cap=100,
    bands=(Band("low", 0), Band("medium", 40), Band("high", 70), Band("critical", 90)),
    alert_threshold=70,
)


def hit(code: str, weight: int, force: bool = False) -> RuleHit:
    return RuleHit(uuid.uuid4(), code, weight, Severity.MEDIUM, force, {}, {}, f"{code} happened")


def test_rules_only_score_uses_rule_component_at_full_weight() -> None:
    result = combine([hit("A", 30), hit("B", 25)], SPEC)
    assert result.mode is ScoringMode.RULES_ONLY
    assert result.rule_score == 55 and result.risk_score == 55
    assert result.category == "medium" and result.model_score is None


def test_rule_component_is_capped() -> None:
    capped = replace(SPEC, rule_cap=60)
    result = combine([hit("A", 35), hit("B", 35), hit("C", 35)], capped)
    assert result.rule_score == 60 and result.risk_score == 60


def test_combined_mode() -> None:
    result = combine([hit("A", 40)], SPEC, model_score=0.9)
    assert result.mode is ScoringMode.COMBINED
    assert result.risk_score == round(0.5 * 40 + 0.5 * 90)


def test_force_alert_and_reason_order() -> None:
    result = combine([hit("SMALL", 10), hit("BIG", 35, force=True), hit("AAA", 10)], SPEC)
    assert result.force_alert is True
    assert result.reasons == (
        "BIG happened (rule BIG)",
        "AAA happened (rule AAA)",
        "SMALL happened (rule SMALL)",
    )


def test_no_hits_is_low_risk() -> None:
    result = combine([], SPEC)
    assert result.risk_score == 0 and result.category == "low" and not result.force_alert


@pytest.mark.parametrize(
    ("score", "band"),
    [
        (0, "low"),
        (39, "low"),
        (40, "medium"),
        (69, "medium"),
        (70, "high"),
        (89, "high"),
        (90, "critical"),
        (100, "critical"),
    ],
)
def test_band_boundaries(score: int, band: str) -> None:
    assert band_for(score, SPEC.bands) == band
