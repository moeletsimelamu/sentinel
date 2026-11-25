"""Rule catalogue v1.

Each rule is a pure check over a ``FeatureRow`` with typed, bounded parameters. Admins can
tune parameters, weight, severity, ``force_alert`` and activation (each change is a new
version), but cannot author rule logic — new rule *types* are added here, in reviewed code.

Wording: a rule hit describes *unusual activity*. Explanations never say "fraud".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from sentinel.core.vocabulary import REPORTING_CURRENCY
from sentinel.features.behaviour import FeatureRow


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RuleCategory(StrEnum):
    AMOUNT = "amount"
    VELOCITY = "velocity"
    GEOGRAPHY = "geography"
    DEVICE = "device"
    BEHAVIOUR = "behaviour"


class RuleParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class Observation:
    observed: dict[str, Any]
    threshold: dict[str, Any]
    explanation: str


def money(value: float) -> str:
    return f"{REPORTING_CURRENCY} {value:,.2f}"


def ratio(value: float) -> str:
    return f"{value:.1f}x"


class Rule(ABC):
    code: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[RuleCategory]
    params_schema: ClassVar[type[RuleParams]]
    default_weight: ClassVar[int]
    default_severity: ClassVar[Severity]
    default_force_alert: ClassVar[bool] = False

    @abstractmethod
    def check(self, row: FeatureRow, params: Any) -> Observation | None:
        """Return an observation when the rule triggers, else None. Must be None-safe."""


# ---------------------------------------------------------------- amount


class LargeAmountAbsoluteParams(RuleParams):
    min_amount_zar: float = Field(default=50_000, gt=0, le=10_000_000)


class LargeAmountAbsolute(Rule):
    code = "LARGE_AMOUNT_ABSOLUTE"
    name = "Unusually large transaction"
    description = "Amount (converted to ZAR) at or above a fixed threshold."
    category = RuleCategory.AMOUNT
    params_schema = LargeAmountAbsoluteParams
    default_weight = 30
    default_severity = Severity.HIGH

    def check(self, row: FeatureRow, params: LargeAmountAbsoluteParams) -> Observation | None:
        if row.amount_zar < params.min_amount_zar:
            return None
        return Observation(
            {"amount_zar": round(row.amount_zar, 2)},
            {"min_amount_zar": params.min_amount_zar},
            f"Amount {money(row.amount_zar)} is at or above the large-transaction threshold "
            f"of {money(params.min_amount_zar)}",
        )


class LargeAmountRelativeParams(RuleParams):
    min_multiple: float = Field(default=8, gt=1, le=1000)
    min_history: int = Field(default=10, ge=1, le=1000)


class LargeAmountRelative(Rule):
    code = "LARGE_AMOUNT_RELATIVE"
    name = "Spending far above customer's norm"
    description = "Amount is many times the customer's 90-day median completed amount."
    category = RuleCategory.AMOUNT
    params_schema = LargeAmountRelativeParams
    default_weight = 25
    default_severity = Severity.MEDIUM

    def check(self, row: FeatureRow, params: LargeAmountRelativeParams) -> Observation | None:
        r = row.amount_vs_cust_median_90d
        if r is None or row.history_count_90d < params.min_history or r < params.min_multiple:
            return None
        return Observation(
            {"amount_vs_median": round(r, 2), "history_count_90d": row.history_count_90d},
            {"min_multiple": params.min_multiple, "min_history": params.min_history},
            f"Amount {money(row.amount_zar)} is {ratio(r)} this customer's 90-day median "
            f"(threshold {ratio(params.min_multiple)})",
        )


# -------------------------------------------------------------- velocity


class RapidRepeatParams(RuleParams):
    min_count: int = Field(default=3, ge=2, le=100)


class RapidRepeat(Rule):
    code = "RAPID_REPEAT"
    name = "Rapid repeated transactions"
    description = "Several transactions in the same merchant category within 10 minutes."
    category = RuleCategory.VELOCITY
    params_schema = RapidRepeatParams
    default_weight = 20
    default_severity = Severity.MEDIUM

    def check(self, row: FeatureRow, params: RapidRepeatParams) -> Observation | None:
        count = row.same_category_count_10m + 1  # including this transaction
        if count < params.min_count:
            return None
        return Observation(
            {"count_10m_same_category": count},
            {"min_count": params.min_count},
            f"{count} transactions in category '{row.merchant_category}' within 10 minutes, "
            f"including this one (threshold {params.min_count})",
        )


class Velocity1hParams(RuleParams):
    min_prior_count: int = Field(default=6, ge=1, le=1000)


class Velocity1h(Rule):
    code = "VELOCITY_1H"
    name = "Unusual transaction frequency"
    description = "Many transactions by the customer in the preceding hour."
    category = RuleCategory.VELOCITY
    params_schema = Velocity1hParams
    default_weight = 20
    default_severity = Severity.MEDIUM

    def check(self, row: FeatureRow, params: Velocity1hParams) -> Observation | None:
        if row.txn_count_1h < params.min_prior_count:
            return None
        return Observation(
            {"txn_count_1h": row.txn_count_1h},
            {"min_prior_count": params.min_prior_count},
            f"{row.txn_count_1h} transactions in the hour before this one "
            f"(threshold {params.min_prior_count})",
        )


class VelocityRatio24hParams(RuleParams):
    min_ratio: float = Field(default=5, gt=1, le=1000)
    min_prior_count_24h: int = Field(default=5, ge=1, le=1000)


class VelocityRatio24h(Rule):
    code = "VELOCITY_RATIO_24H"
    name = "Frequency far above customer's baseline"
    description = "Last-24h transaction count compared with the customer's 30-day daily average."
    category = RuleCategory.VELOCITY
    params_schema = VelocityRatio24hParams
    default_weight = 15
    default_severity = Severity.MEDIUM

    def check(self, row: FeatureRow, params: VelocityRatio24hParams) -> Observation | None:
        r = row.velocity_ratio_24h_vs_30d
        if r is None or r < params.min_ratio or row.txn_count_24h < params.min_prior_count_24h:
            return None
        return Observation(
            {"velocity_ratio": round(r, 2), "txn_count_24h": row.txn_count_24h},
            {"min_ratio": params.min_ratio, "min_prior_count_24h": params.min_prior_count_24h},
            f"{row.txn_count_24h} transactions in the previous 24 hours, {ratio(r)} this "
            f"customer's 30-day daily average (threshold {ratio(params.min_ratio)})",
        )


class FailedAttemptsParams(RuleParams):
    min_failed: int = Field(default=3, ge=1, le=1000)


class FailedAttempts(Rule):
    code = "FAILED_ATTEMPTS"
    name = "Multiple failed transactions"
    description = "Declined or failed attempts by the customer in the preceding 24 hours."
    category = RuleCategory.VELOCITY
    params_schema = FailedAttemptsParams
    default_weight = 25
    default_severity = Severity.HIGH

    def check(self, row: FeatureRow, params: FailedAttemptsParams) -> Observation | None:
        if row.failed_count_24h < params.min_failed:
            return None
        return Observation(
            {"failed_count_24h": row.failed_count_24h},
            {"min_failed": params.min_failed},
            f"{row.failed_count_24h} declined or failed attempts in the previous 24 hours "
            f"(threshold {params.min_failed})",
        )


# ------------------------------------------------------------- geography


class ImpossibleTravelParams(RuleParams):
    min_speed_kmh: float = Field(default=900, gt=0, le=20_000)
    min_distance_km: float = Field(default=300, gt=0, le=20_000)


class ImpossibleTravel(Rule):
    code = "IMPOSSIBLE_TRAVEL"
    name = "Implausible travel between transactions"
    description = "Distance from the previous located transaction implies an unrealistic speed."
    category = RuleCategory.GEOGRAPHY
    params_schema = ImpossibleTravelParams
    default_weight = 35
    default_severity = Severity.HIGH
    default_force_alert = True

    def check(self, row: FeatureRow, params: ImpossibleTravelParams) -> Observation | None:
        km, speed = row.km_from_prev_txn, row.implied_speed_kmh
        if km is None or speed is None:
            return None
        if km < params.min_distance_km or speed < params.min_speed_kmh:
            return None
        return Observation(
            {"km_from_prev_txn": round(km, 1), "implied_speed_kmh": round(speed, 1)},
            {"min_distance_km": params.min_distance_km, "min_speed_kmh": params.min_speed_kmh},
            f"{km:,.0f} km from the previous transaction, implying {speed:,.0f} km/h "
            f"(threshold {params.min_speed_kmh:,.0f} km/h over at least "
            f"{params.min_distance_km:,.0f} km)",
        )


class NewLocationHighValueParams(RuleParams):
    min_multiple: float = Field(default=3, gt=1, le=1000)
    min_history: int = Field(default=5, ge=1, le=1000)


class NewLocationHighValue(Rule):
    code = "NEW_LOCATION_HIGH_VALUE"
    name = "High value in a new city"
    description = "First transaction in this city for 90 days, at well above the usual amount."
    category = RuleCategory.GEOGRAPHY
    params_schema = NewLocationHighValueParams
    default_weight = 15
    default_severity = Severity.LOW

    def check(self, row: FeatureRow, params: NewLocationHighValueParams) -> Observation | None:
        r = row.amount_vs_cust_median_90d
        if not row.is_new_city_90d or r is None or row.history_count_90d < params.min_history:
            return None
        if r < params.min_multiple:
            return None
        return Observation(
            {"is_new_city_90d": True, "amount_vs_median": round(r, 2)},
            {"min_multiple": params.min_multiple, "min_history": params.min_history},
            f"First transaction in this city in 90 days, at {ratio(r)} the customer's "
            f"median amount (threshold {ratio(params.min_multiple)})",
        )


# ------------------------------------------------------- device/behaviour


class UnusualHourParams(RuleParams):
    min_rarity: float = Field(default=0.97, gt=0, le=1)
    min_multiple: float = Field(default=2, ge=1, le=1000)
    min_history: int = Field(default=10, ge=1, le=1000)


class UnusualHour(Rule):
    code = "UNUSUAL_HOUR"
    name = "Unusual time of day"
    description = "Hour the customer is rarely active, at above the usual amount."
    category = RuleCategory.BEHAVIOUR
    params_schema = UnusualHourParams
    default_weight = 10
    default_severity = Severity.LOW

    def check(self, row: FeatureRow, params: UnusualHourParams) -> Observation | None:
        rarity, r = row.hour_rarity_cust, row.amount_vs_cust_median_90d
        if rarity is None or r is None or row.history_count_90d < params.min_history:
            return None
        if rarity < params.min_rarity or r < params.min_multiple:
            return None
        share = 1 - rarity
        return Observation(
            {
                "hour_utc": row.hour_utc,
                "hour_rarity": round(rarity, 3),
                "amount_vs_median": round(r, 2),
            },
            {
                "min_rarity": params.min_rarity,
                "min_multiple": params.min_multiple,
                "min_history": params.min_history,
            },
            f"Only {share:.0%} of this customer's recent transactions were within an hour of "
            f"{row.hour_utc:02d}:00 UTC; amount is {ratio(r)} their median",
        )


class NewDeviceHighValueParams(RuleParams):
    min_multiple: float = Field(default=3, gt=1, le=1000)
    min_history: int = Field(default=5, ge=1, le=1000)


class NewDeviceHighValue(Rule):
    code = "NEW_DEVICE_HIGH_VALUE"
    name = "High value from a new device"
    description = "First use of this device in 90 days, at well above the usual amount."
    category = RuleCategory.DEVICE
    params_schema = NewDeviceHighValueParams
    default_weight = 20
    default_severity = Severity.MEDIUM

    def check(self, row: FeatureRow, params: NewDeviceHighValueParams) -> Observation | None:
        r = row.amount_vs_cust_median_90d
        if not row.is_new_device_90d or r is None or row.history_count_90d < params.min_history:
            return None
        if r < params.min_multiple:
            return None
        return Observation(
            {"is_new_device_90d": True, "amount_vs_median": round(r, 2)},
            {"min_multiple": params.min_multiple, "min_history": params.min_history},
            f"First transaction from this device in 90 days, at {ratio(r)} the customer's "
            f"median amount (threshold {ratio(params.min_multiple)})",
        )


class HighRiskCategoryShiftParams(RuleParams):
    min_history: int = Field(default=5, ge=1, le=1000)


class HighRiskCategoryShift(Rule):
    code = "HIGH_RISK_CATEGORY_SHIFT"
    name = "First use of a high-risk category"
    description = "First transaction in 90 days in a category often used to move funds out."
    category = RuleCategory.BEHAVIOUR
    params_schema = HighRiskCategoryShiftParams
    default_weight = 20
    default_severity = Severity.MEDIUM

    def check(self, row: FeatureRow, params: HighRiskCategoryShiftParams) -> Observation | None:
        if not (row.high_risk_category and row.is_new_category_90d):
            return None
        if row.history_count_90d < params.min_history:
            return None
        return Observation(
            {"merchant_category": row.merchant_category, "is_new_category_90d": True},
            {"min_history": params.min_history},
            f"First '{row.merchant_category}' transaction for this customer in 90 days",
        )


class BehaviourShiftParams(RuleParams):
    min_shift: float = Field(default=0.5, gt=0, le=1)
    min_history: int = Field(default=20, ge=1, le=1000)


class BehaviourShift(Rule):
    code = "BEHAVIOUR_SHIFT"
    name = "Change in spending mix"
    description = "Last-7-day category mix differs strongly from the 90-day pattern."
    category = RuleCategory.BEHAVIOUR
    params_schema = BehaviourShiftParams
    default_weight = 10
    default_severity = Severity.LOW

    def check(self, row: FeatureRow, params: BehaviourShiftParams) -> Observation | None:
        shift = row.category_share_shift_7d_vs_90d
        if shift is None or row.history_count_90d < params.min_history:
            return None
        if shift < params.min_shift:
            return None
        return Observation(
            {"category_shift": round(shift, 3), "history_count_90d": row.history_count_90d},
            {"min_shift": params.min_shift, "min_history": params.min_history},
            f"Spending mix over the last 7 days differs from the 90-day pattern "
            f"(distance {shift:.2f}, threshold {params.min_shift:.2f})",
        )


CATALOGUE: dict[str, Rule] = {
    r.code: r
    for r in (
        LargeAmountAbsolute(),
        LargeAmountRelative(),
        RapidRepeat(),
        Velocity1h(),
        VelocityRatio24h(),
        FailedAttempts(),
        ImpossibleTravel(),
        NewLocationHighValue(),
        UnusualHour(),
        NewDeviceHighValue(),
        HighRiskCategoryShift(),
        BehaviourShift(),
    )
}
