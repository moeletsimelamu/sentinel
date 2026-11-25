"""Behavioural features computed from a customer's own history.

**Leakage rule:** every aggregate for a transaction at time ``T`` uses only that
customer's transactions with ``occurred_at < T`` (strictly earlier; same-instant
transactions do not see each other). The transaction's own attributes (amount,
category, location, …) are of course available. This is enforced structurally in
``build_feature_rows``: transactions are added to the sliding windows only after every
transaction at the same instant has been scored.

Windows are sliding (``[T - d, T)``), maintained incrementally per customer, so the cost
is linear in the number of transactions.
"""

from __future__ import annotations

import bisect
import math
import uuid
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sentinel.core.vocabulary import FX_TO_ZAR, HIGH_RISK_CATEGORIES, TxnStatus, TxnType

FEATURE_SET_VERSION = "behaviour-v2"

FAILED_STATUSES = frozenset({TxnStatus.DECLINED.value, TxnStatus.FAILED.value})
HIGH_RISK = frozenset(c.value for c in HIGH_RISK_CATEGORIES)
INFLOW_TYPES = frozenset({TxnType.TRANSFER_IN.value, TxnType.REFUND.value})
MIN_ELAPSED_HOURS = 1 / 60  # one minute: avoids infinite speeds for near-simultaneous events
MAX_SPEED_KMH = 20_000.0
EARTH_RADIUS_KM = 6371.0088

W_10M = timedelta(minutes=10)
W_1H = timedelta(hours=1)
W_24H = timedelta(hours=24)
W_7D = timedelta(days=7)
W_30D = timedelta(days=30)
W_90D = timedelta(days=90)


@dataclass(frozen=True)
class TxnRecord:
    """The fields of a stored transaction that features are computed from."""

    id: uuid.UUID
    customer_id: uuid.UUID
    occurred_at: datetime
    amount: float
    currency: str
    status: str
    merchant_category: str
    channel: str
    city: str | None
    lat: float | None
    lon: float | None
    device_id_hash: str | None
    txn_type: str = TxnType.PURCHASE.value

    @property
    def is_inflow(self) -> bool:
        return self.txn_type in INFLOW_TYPES

    @property
    def amount_zar(self) -> float:
        return self.amount * FX_TO_ZAR[self.currency]


@dataclass(frozen=True)
class FeatureRow:
    """Features for one transaction. ``None`` means "unknown" (e.g. no history yet)."""

    transaction_id: uuid.UUID
    amount: float
    currency: str
    amount_zar: float
    hour_utc: int
    day_of_week: int  # Monday = 0 (UTC)
    merchant_category: str
    channel: str
    txn_type: str
    high_risk_category: bool
    # history (prior 90 days unless stated)
    history_count_90d: int
    history_span_days_90d: float | None  # days since the earliest txn in the window
    secs_since_prev_txn: float | None
    cust_median_amount_zar_90d: float | None  # completed transactions only
    amount_vs_cust_median_90d: float | None
    # velocity (prior transactions, any status)
    txn_count_1h: int
    txn_count_24h: int
    txn_count_30d: int
    velocity_ratio_24h_vs_30d: float | None
    failed_count_24h: int
    same_category_count_10m: int
    spend_sum_24h_zar: float  # prior completed outgoing amounts
    inflow_sum_24h_zar: float  # prior completed incoming transfers/refunds
    # geography
    km_from_prev_txn: float | None
    implied_speed_kmh: float | None
    is_new_city_90d: bool | None
    # device / behaviour
    is_new_device_90d: bool | None
    is_new_category_90d: bool | None
    channel_rarity_cust: float | None  # 1 - share of prior 90d txns on this channel
    hour_rarity_cust: float | None
    category_share_shift_7d_vs_90d: float | None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["transaction_id"] = str(self.transaction_id)
        return out


def _utc(t: TxnRecord) -> datetime:
    """Calendar fields (hour, weekday) always in UTC, whatever timezone the source used."""
    return t.occurred_at.astimezone(UTC)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def js_distance(p: Counter[str], q: Counter[str]) -> float:
    """Jensen-Shannon distance (base 2) between two category count distributions, in [0, 1]."""
    total_p, total_q = sum(p.values()), sum(q.values())
    keys = set(p) | set(q)
    divergence = 0.0
    for k in keys:
        pk, qk = p[k] / total_p, q[k] / total_q
        mk = (pk + qk) / 2
        if pk > 0:
            divergence += 0.5 * pk * math.log2(pk / mk)
        if qk > 0:
            divergence += 0.5 * qk * math.log2(qk / mk)
    return math.sqrt(max(divergence, 0.0))


class _Window:
    """Transactions in ``[T - span, T)`` for the current ``T``."""

    def __init__(self, span: timedelta) -> None:
        self.span = span
        self.items: deque[TxnRecord] = deque()

    def add(self, txn: TxnRecord) -> None:
        self.items.append(txn)

    def evict(self, now: datetime) -> list[TxnRecord]:
        cutoff = now - self.span
        removed = []
        while self.items and self.items[0].occurred_at < cutoff:
            removed.append(self.items.popleft())
        return removed

    def __len__(self) -> int:
        return len(self.items)


class _CustomerState:
    def __init__(self) -> None:
        self.w10m, self.w1h, self.w24h = _Window(W_10M), _Window(W_1H), _Window(W_24H)
        self.w7d, self.w30d, self.w90d = _Window(W_7D), _Window(W_30D), _Window(W_90D)
        self.categories_10m: Counter[str] = Counter()
        self.categories_7d: Counter[str] = Counter()
        self.categories_90d: Counter[str] = Counter()
        self.cities_90d: Counter[str] = Counter()
        self.devices_90d: Counter[str] = Counter()
        self.hours_90d: Counter[int] = Counter()
        self.channels_90d: Counter[str] = Counter()
        self.failed_24h = 0
        self.completed_amounts_90d: list[float] = []  # kept sorted for the median
        self.last_located: tuple[datetime, float, float] | None = None  # (time, lat, lon)

    def add(self, t: TxnRecord) -> None:
        for w in (self.w10m, self.w1h, self.w24h, self.w7d, self.w30d, self.w90d):
            w.add(t)
        self.categories_10m[t.merchant_category] += 1
        self.categories_7d[t.merchant_category] += 1
        self.categories_90d[t.merchant_category] += 1
        if t.city:
            self.cities_90d[t.city.casefold()] += 1
        if t.device_id_hash:
            self.devices_90d[t.device_id_hash] += 1
        self.hours_90d[_utc(t).hour] += 1
        self.channels_90d[t.channel] += 1
        if t.status in FAILED_STATUSES:
            self.failed_24h += 1
        if t.status == TxnStatus.COMPLETED:
            bisect.insort(self.completed_amounts_90d, t.amount_zar)
        if t.lat is not None and t.lon is not None:
            self.last_located = (t.occurred_at, t.lat, t.lon)

    def evict(self, now: datetime) -> None:
        self.w1h.evict(now)
        self.w30d.evict(now)
        for t in self.w10m.evict(now):
            _dec(self.categories_10m, t.merchant_category)
        for t in self.w24h.evict(now):
            if t.status in FAILED_STATUSES:
                self.failed_24h -= 1
        for t in self.w7d.evict(now):
            _dec(self.categories_7d, t.merchant_category)
        for t in self.w90d.evict(now):
            _dec(self.categories_90d, t.merchant_category)
            if t.city:
                _dec(self.cities_90d, t.city.casefold())
            if t.device_id_hash:
                _dec(self.devices_90d, t.device_id_hash)
            _dec(self.hours_90d, _utc(t).hour)
            _dec(self.channels_90d, t.channel)
            if t.status == TxnStatus.COMPLETED:
                i = bisect.bisect_left(self.completed_amounts_90d, t.amount_zar)
                del self.completed_amounts_90d[i]

    def median_amount(self) -> float | None:
        values = self.completed_amounts_90d
        if not values:
            return None
        mid = len(values) // 2
        return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2

    def features(self, t: TxnRecord) -> FeatureRow:
        history = len(self.w90d)
        has_history = history > 0
        median = self.median_amount()
        count_30d = len(self.w30d)
        count_24h = len(self.w24h)

        km = speed = None
        if self.last_located is not None and t.lat is not None and t.lon is not None:
            prev_at, prev_lat, prev_lon = self.last_located
            km = haversine_km(prev_lat, prev_lon, t.lat, t.lon)
            hours = max((t.occurred_at - prev_at).total_seconds() / 3600, MIN_ELAPSED_HOURS)
            speed = min(km / hours, MAX_SPEED_KMH)

        hour = _utc(t).hour
        rarity = None
        if has_history:
            near = sum(self.hours_90d[(hour + d) % 24] for d in (-1, 0, 1))
            rarity = 1 - near / history

        shift = None
        if has_history:
            recent = self.categories_7d.copy()
            recent[t.merchant_category] += 1  # recent behaviour includes this transaction
            shift = js_distance(recent, self.categories_90d)

        # Summed afresh in time order (not maintained incrementally) so the result is
        # bit-identical however much older history was loaded.
        completed_24h = [x for x in self.w24h.items if x.status == TxnStatus.COMPLETED]
        spend_24h = sum(x.amount_zar for x in completed_24h if not x.is_inflow)
        inflow_24h = sum(x.amount_zar for x in completed_24h if x.is_inflow)

        span = secs_prev = channel_rarity = None
        if has_history:
            window = self.w90d.items
            span = (t.occurred_at - window[0].occurred_at).total_seconds() / 86400
            secs_prev = (t.occurred_at - window[-1].occurred_at).total_seconds()
            channel_rarity = 1 - self.channels_90d[t.channel] / history

        return FeatureRow(
            transaction_id=t.id,
            amount=t.amount,
            currency=t.currency,
            amount_zar=t.amount_zar,
            hour_utc=hour,
            day_of_week=_utc(t).weekday(),
            merchant_category=t.merchant_category,
            channel=t.channel,
            txn_type=t.txn_type,
            high_risk_category=t.merchant_category in HIGH_RISK,
            history_count_90d=history,
            history_span_days_90d=span,
            secs_since_prev_txn=secs_prev,
            cust_median_amount_zar_90d=median,
            amount_vs_cust_median_90d=(t.amount_zar / median) if median else None,
            txn_count_1h=len(self.w1h),
            txn_count_24h=count_24h,
            txn_count_30d=count_30d,
            velocity_ratio_24h_vs_30d=(count_24h / (count_30d / 30)) if count_30d else None,
            failed_count_24h=self.failed_24h,
            same_category_count_10m=self.categories_10m[t.merchant_category],
            spend_sum_24h_zar=spend_24h,
            inflow_sum_24h_zar=inflow_24h,
            km_from_prev_txn=km,
            implied_speed_kmh=speed,
            is_new_city_90d=(
                (t.city.casefold() not in self.cities_90d) if (t.city and has_history) else None
            ),
            is_new_device_90d=(
                (t.device_id_hash not in self.devices_90d)
                if (t.device_id_hash and has_history)
                else None
            ),
            is_new_category_90d=(
                (t.merchant_category not in self.categories_90d) if has_history else None
            ),
            channel_rarity_cust=channel_rarity,
            hour_rarity_cust=rarity,
            category_share_shift_7d_vs_90d=shift,
        )


def _dec[K](counter: Counter[K], key: K) -> None:
    counter[key] -= 1
    if counter[key] <= 0:
        del counter[key]


def build_feature_rows(
    history: Iterable[TxnRecord], target_ids: set[uuid.UUID] | None = None
) -> dict[uuid.UUID, FeatureRow]:
    """Compute features for ``target_ids`` (default: every transaction in ``history``).

    ``history`` must contain, for each target's customer, all transactions in the 90 days
    before the target; extra transactions (later ones, other customers) are harmless.
    """
    by_customer: dict[uuid.UUID, list[TxnRecord]] = {}
    for t in history:
        by_customer.setdefault(t.customer_id, []).append(t)

    out: dict[uuid.UUID, FeatureRow] = {}
    for txns in by_customer.values():
        txns.sort(key=lambda t: (t.occurred_at, t.id))
        state = _CustomerState()
        i = 0
        n = len(txns)
        while i < n:
            now = txns[i].occurred_at
            # All transactions sharing this instant are scored against the same history,
            # then added together — none of them sees the others.
            j = i
            while j < n and txns[j].occurred_at == now:
                j += 1
            state.evict(now)
            for t in txns[i:j]:
                if target_ids is None or t.id in target_ids:
                    out[t.id] = state.features(t)
            for t in txns[i:j]:
                state.add(t)
            i = j
    return out
