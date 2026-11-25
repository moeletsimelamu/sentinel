"""Customers, legitimate activity and benign anomalies.

Customers carry behavioural parameters only — no names, ages, genders, ethnicities,
nationalities or addresses. ``segment`` is a spending archetype used to drive the
simulation; it is written to ``customers.parquet`` for analysis but never ingested and
never used as a model input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from sentinel_ml.generate.catalog import (
    AMOUNTS,
    CATEGORY_ALPHA,
    CITIES,
    DIGITAL_ONLY,
    FOREIGN_CITIES,
    FX_FROM_ZAR,
    HOME_CITIES,
    IN_PERSON,
    NEVER_DIGITAL,
    SEGMENTS,
    SPEND_CATEGORIES,
    TRAVEL,
    City,
)
from sentinel_ml.generate.profiles import Profile

LOCAL_UTC_OFFSET_H = 2  # simulated customers live on South African time (UTC+2)
CITY_BY_NAME = {c.name: c for c in CITIES}


@dataclass
class Customer:
    ref: str
    segment: str
    home: City
    rate: float
    scale: float
    digital: float
    mobile_share: float
    prefs: np.ndarray
    hour_peaks: tuple[float, float]
    evening_weight: float
    devices: list[str]
    shift_worker: bool
    salary_zar: float | None
    # (switch time, new device) for the benign "new phone" scenario
    new_phone: tuple[datetime, str] | None = None
    trips: list[tuple[datetime, datetime, City]] = field(default_factory=list)

    @property
    def typical_amount_zar(self) -> float:
        return 400.0 * self.scale


class Sink:
    """Column-oriented transaction buffer (much lighter than one dict per row)."""

    COLUMNS = (
        "customer_ref",
        "occurred_at",
        "amount",
        "currency",
        "txn_type",
        "channel",
        "merchant_category",
        "status",
        "city",
        "country",
        "lat",
        "lon",
        "device_id",
        "scenario_id",
        "typology",
        "benign_scenario",
    )

    def __init__(self) -> None:
        self.cols: dict[str, list[Any]] = {c: [] for c in self.COLUMNS}

    def __len__(self) -> int:
        return len(self.cols["customer_ref"])

    def emit(
        self,
        rng: np.random.Generator,
        cust: Customer,
        ts: datetime,
        amount_zar: float,
        category: str,
        *,
        city: City | None = None,
        channel: str | None = None,
        txn_type: str | None = None,
        device: str | None = None,
        status: str | None = None,
        scenario_id: str | None = None,
        typology: str | None = None,
        benign: str | None = None,
    ) -> None:
        city = city or cust.home
        txn_type = txn_type or _txn_type(category)
        channel = channel or _channel(rng, cust, category, txn_type)
        digital = channel in ("online", "mobile_app", "bank_transfer")
        if digital and device is None:
            device = current_device(rng, cust, ts)
        if not digital:
            device = None
        located = channel in ("card_present", "atm") or (digital and rng.random() < 0.7)
        currency = city.currency if channel in ("card_present", "atm") else "ZAR"
        amount = max(0.01, round(amount_zar * FX_FROM_ZAR[currency], 2))
        if status is None:
            u = rng.random()
            status = "completed" if u < 0.975 else ("declined" if u < 0.995 else "failed")
        jitter = 0.03 if located else 0.0
        c = self.cols
        c["customer_ref"].append(cust.ref)
        c["occurred_at"].append(ts)
        c["amount"].append(amount)
        c["currency"].append(currency)
        c["txn_type"].append(txn_type)
        c["channel"].append(channel)
        c["merchant_category"].append(category)
        c["status"].append(status)
        c["city"].append(city.name if (located or not digital) else None)
        c["country"].append(city.country if (located or not digital) else None)
        c["lat"].append(round(city.lat + rng.uniform(-jitter, jitter), 4) if located else None)
        c["lon"].append(round(city.lon + rng.uniform(-jitter, jitter), 4) if located else None)
        c["device_id"].append(device)
        c["scenario_id"].append(scenario_id)
        c["typology"].append(typology)
        c["benign_scenario"].append(benign)


def _txn_type(category: str) -> str:
    if category == "atm_cash":
        return "atm_withdrawal"
    if category == "money_transfer":
        return "transfer_out"
    return "purchase"


def _channel(rng: np.random.Generator, cust: Customer, category: str, txn_type: str) -> str:
    if txn_type == "atm_withdrawal":
        return "atm"
    if txn_type in ("transfer_out", "transfer_in"):
        return "bank_transfer" if rng.random() < 0.6 else "mobile_app"
    if category in NEVER_DIGITAL:
        return "card_present"
    digital = (
        category in DIGITAL_ONLY
        or (category not in IN_PERSON and rng.random() < 0.8)
        or rng.random() < cust.digital * 0.5
    )
    if not digital:
        return "card_present"
    return "mobile_app" if rng.random() < cust.mobile_share else "online"


def current_device(rng: np.random.Generator, cust: Customer, ts: datetime) -> str:
    if cust.new_phone is not None and ts >= cust.new_phone[0]:
        return cust.new_phone[1] if rng.random() < 0.9 else cust.devices[-1]
    return cust.devices[int(rng.integers(len(cust.devices)))]


def sample_amount(rng: np.random.Generator, cust: Customer, category: str) -> float:
    median, sigma = AMOUNTS[category]
    return float(median * cust.scale * math.exp(rng.normal(0, sigma)))


def make_customers(rng: np.random.Generator, profile: Profile) -> list[Customer]:
    names = list(SEGMENTS)
    shares = np.array([SEGMENTS[s]["share"] for s in names])
    home_w = np.array([c.home_weight for c in HOME_CITIES])
    alpha = np.array([CATEGORY_ALPHA[c] for c in SPEND_CATEGORIES])
    start = datetime.combine(profile.start, datetime.min.time(), tzinfo=UTC)
    customers = []
    for i in range(profile.n_customers):
        seg = names[int(rng.choice(len(names), p=shares / shares.sum()))]
        s = SEGMENTS[seg]
        shift = bool(rng.random() < profile.benign["shift_worker"])
        morning = rng.uniform(1.0, 3.5) if shift else rng.uniform(9.5, 13.5)
        evening = rng.uniform(4.0, 6.0) if shift else rng.uniform(17.0, 20.0)
        devices = [f"dev-{i:05d}-{k}" for k in range(int(rng.integers(1, 4)))]
        cust = Customer(
            ref=f"C-{i + 1:05d}",
            segment=seg,
            home=HOME_CITIES[int(rng.choice(len(HOME_CITIES), p=home_w / home_w.sum()))],
            rate=float(rng.gamma(4.0, s["rate"] / 4.0)),
            scale=float(s["scale"] * math.exp(rng.normal(0, 0.35))),
            digital=float(np.clip(rng.normal(s["digital"], 0.12), 0.02, 0.98)),
            mobile_share=float(rng.uniform(0.2, 0.8)),
            prefs=rng.dirichlet(alpha * (1.5 if seg == "frequent_spender" else 1.0)),
            hour_peaks=(morning, evening),
            evening_weight=float(rng.uniform(0.2, 0.6)),
            devices=devices,
            shift_worker=shift,
            salary_zar=(
                None
                if seg == "low_activity" and rng.random() < 0.5
                else float(18000 * s["scale"] * math.exp(rng.normal(0, 0.4)))
            ),
        )
        if rng.random() < profile.benign["new_phone"]:
            switch = start + timedelta(days=float(rng.uniform(10, profile.days - 5)))
            cust.new_phone = (switch, f"dev-{i:05d}-new")
        trips_per_month = profile.benign[
            "trips_per_month_traveller" if seg == "traveller" else "trips_per_month_other"
        ]
        for _ in range(int(rng.poisson(trips_per_month * profile.days / 30))):
            begin = start + timedelta(days=float(rng.uniform(7, max(8, profile.days - 9))))
            length = timedelta(days=float(rng.uniform(3, 8)))
            pool = FOREIGN_CITIES if rng.random() < 0.6 else HOME_CITIES
            dest = pool[int(rng.integers(len(pool)))]
            if dest.name != cust.home.name:
                cust.trips.append((begin, begin + length, dest))
        customers.append(cust)
    return customers


def _local_hour(rng: np.random.Generator, cust: Customer) -> float:
    peak = cust.hour_peaks[1] if rng.random() < cust.evening_weight else cust.hour_peaks[0]
    return float(rng.normal(peak, 1.6)) % 24


def _location(cust: Customer, ts: datetime) -> tuple[City, bool]:
    for begin, end, dest in cust.trips:
        if begin <= ts < end:
            return dest, True
    return cust.home, False


def simulate_customer(
    rng: np.random.Generator, cust: Customer, profile: Profile, sink: Sink
) -> None:
    """Everyday activity plus this customer's benign anomalies."""
    start = datetime.combine(profile.start, datetime.min.time(), tzinfo=UTC)
    for d in range(profile.days):
        day = start + timedelta(days=d)
        factor = 1.25 if day.weekday() >= 5 else (1.1 if day.weekday() == 4 else 1.0)
        if 25 <= day.day <= 27:
            factor *= 1.4  # payday
        if cust.salary_zar and day.day == 25:
            sink.emit(
                rng,
                cust,
                day + timedelta(hours=4, minutes=float(rng.uniform(0, 60))),
                cust.salary_zar,
                "payroll",
                txn_type="transfer_in",
                channel="bank_transfer",
                status="completed",
            )
        for _ in range(int(rng.poisson(cust.rate * factor))):
            hour = _local_hour(rng, cust)
            ts = day + timedelta(hours=hour - LOCAL_UTC_OFFSET_H)
            city, travelling = _location(cust, ts)
            if travelling and rng.random() < 0.45:
                category = TRAVEL[int(rng.integers(len(TRAVEL)))]
            else:
                category = SPEND_CATEGORIES[int(rng.choice(len(SPEND_CATEGORIES), p=cust.prefs))]
            sink.emit(
                rng,
                cust,
                ts,
                sample_amount(rng, cust, category),
                category,
                city=city,
                benign="travel" if travelling else None,
            )

    for begin, _, dest in cust.trips:  # flight booked from home shortly before the trip
        sink.emit(
            rng,
            cust,
            begin - timedelta(days=float(rng.uniform(2, 20))),
            sample_amount(rng, cust, "travel_airline") * (2 if dest in FOREIGN_CITIES else 1),
            "travel_airline",
            channel="online",
            benign="travel",
        )
    end = start + timedelta(days=profile.days)
    if rng.random() < profile.benign["big_ticket"]:
        ts = start + timedelta(days=float(rng.uniform(10, profile.days - 1)), hours=10)
        category = "electronics" if rng.random() < 0.6 else "home_furniture"
        sink.emit(
            rng,
            cust,
            min(ts, end - timedelta(hours=1)),
            cust.typical_amount_zar * float(rng.uniform(5, 20)),
            category,
            channel="card_present",
            status="completed",
            benign="big_ticket",
        )
    if rng.random() < profile.benign["payday_splurge"]:
        paydays = [start + timedelta(days=d) for d in range(profile.days)]
        paydays = [p for p in paydays if p.day == 25]
        if paydays:
            p = paydays[int(rng.integers(len(paydays)))] + timedelta(hours=15)
            for k in range(int(rng.integers(4, 9))):
                category = ("retail_clothing", "restaurants", "entertainment")[k % 3]
                sink.emit(
                    rng,
                    cust,
                    p + timedelta(minutes=float(rng.uniform(0, 180))),
                    sample_amount(rng, cust, category) * 1.5,
                    category,
                    benign="payday_splurge",
                )
