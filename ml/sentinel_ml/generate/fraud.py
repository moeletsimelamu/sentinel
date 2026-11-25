"""Injected fraud episodes.

Episodes are deliberately noisy: amounts, timing, devices and locations are sampled from
ranges that overlap legitimate behaviour (and the benign anomalies), so neither rules nor
models can rediscover a clean signature. ``low_and_slow`` is built to be hard.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import numpy as np

from sentinel_ml.generate.catalog import FOREIGN_CITIES, HOME_CITIES, SPEND_CATEGORIES, City
from sentinel_ml.generate.simulate import Customer, Sink, current_device, sample_amount

Episode = Callable[[np.random.Generator, Customer, datetime, str, Sink], None]


def _other_city(rng: np.random.Generator, cust: Customer, foreign_p: float = 0.5) -> City:
    pool = FOREIGN_CITIES if rng.random() < foreign_p else HOME_CITIES
    choices = [c for c in pool if c.name != cust.home.name]
    return choices[int(rng.integers(len(choices)))]


def _minutes(m: float) -> timedelta:
    return timedelta(minutes=m)


def account_takeover(
    rng: np.random.Generator, cust: Customer, t0: datetime, sid: str, sink: Sink
) -> None:
    device = f"atk-{sid}" if rng.random() < 0.8 else current_device(rng, cust, t0)
    city = _other_city(rng, cust) if rng.random() < 0.6 else cust.home
    ts = t0
    for _ in range(int(rng.integers(3, 7))):
        ts += _minutes(float(rng.uniform(5, 70)))
        category = ("electronics", "online_marketplace", "gift_cards", "money_transfer")[
            int(rng.integers(4))
        ]
        sink.emit(
            rng,
            cust,
            ts,
            cust.typical_amount_zar * float(rng.uniform(3, 15)),
            category,
            city=city,
            channel="bank_transfer" if category == "money_transfer" else "online",
            device=device,
            status="declined" if rng.random() < 0.1 else "completed",
            scenario_id=sid,
            typology="account_takeover",
        )


def card_testing(
    rng: np.random.Generator, cust: Customer, t0: datetime, sid: str, sink: Sink
) -> None:
    device = f"bot-{sid}" if rng.random() < 0.7 else current_device(rng, cust, t0)
    ts = t0
    for _ in range(int(rng.integers(4, 13))):
        ts += _minutes(float(rng.uniform(0.5, 4)))
        sink.emit(
            rng,
            cust,
            ts,
            float(rng.uniform(1, 25)),
            ("online_marketplace", "subscriptions", "gift_cards")[int(rng.integers(3))],
            channel="online",
            device=device,
            status="declined" if rng.random() < 0.5 else "completed",
            scenario_id=sid,
            typology="card_testing",
        )
    for _ in range(int(rng.integers(1, 4))):
        ts += _minutes(float(rng.uniform(2, 30)))
        sink.emit(
            rng,
            cust,
            ts,
            float(rng.uniform(800, 6000)),
            ("electronics", "gift_cards")[int(rng.integers(2))],
            channel="online",
            device=device,
            status="completed",
            scenario_id=sid,
            typology="card_testing",
        )


def impossible_travel(
    rng: np.random.Generator, cust: Customer, t0: datetime, sid: str, sink: Sink
) -> None:
    # A card-present purchase at home, then card-present use far away shortly after.
    sink.emit(
        rng,
        cust,
        t0,
        sample_amount(rng, cust, "groceries"),
        "groceries",
        channel="card_present",
        status="completed",
        scenario_id=sid,
        typology="impossible_travel",
    )
    far = _other_city(rng, cust, foreign_p=0.75)
    ts = t0
    for _ in range(int(rng.integers(1, 4))):
        ts += _minutes(float(rng.uniform(10, 90)))
        category = ("electronics", "retail_clothing", "atm_cash")[int(rng.integers(3))]
        sink.emit(
            rng,
            cust,
            ts,
            sample_amount(rng, cust, category) * float(rng.uniform(1, 4)),
            category,
            city=far,
            status="completed",
            scenario_id=sid,
            typology="impossible_travel",
        )


def high_risk_category_shift(
    rng: np.random.Generator, cust: Customer, t0: datetime, sid: str, sink: Sink
) -> None:
    ts = t0
    for _ in range(int(rng.integers(2, 6))):
        ts += timedelta(hours=float(rng.uniform(0.2, 12)))
        category = ("gift_cards", "crypto_exchange", "money_transfer")[int(rng.integers(3))]
        sink.emit(
            rng,
            cust,
            ts,
            cust.typical_amount_zar * float(rng.uniform(2, 10)),
            category,
            scenario_id=sid,
            typology="high_risk_category_shift",
            status="completed",
        )


def night_drain(
    rng: np.random.Generator, cust: Customer, t0: datetime, sid: str, sink: Sink
) -> None:
    # 00:00-04:00 local time (22:00-02:00 UTC) on the episode's day.
    ts = t0.replace(hour=22, minute=0, second=0, microsecond=0) + _minutes(
        float(rng.uniform(0, 120))
    )
    for _ in range(int(rng.integers(3, 7))):
        ts += _minutes(float(rng.uniform(5, 40)))
        atm = rng.random() < 0.4
        sink.emit(
            rng,
            cust,
            ts,
            cust.typical_amount_zar * float(rng.uniform(3, 12)),
            "atm_cash" if atm else ("electronics", "online_marketplace")[int(rng.integers(2))],
            status="completed",
            scenario_id=sid,
            typology="night_drain",
        )


def mule_passthrough(
    rng: np.random.Generator, cust: Customer, t0: datetime, sid: str, sink: Sink
) -> None:
    inflow = float(rng.uniform(8000, 60000))
    sink.emit(
        rng,
        cust,
        t0,
        inflow,
        "money_transfer",
        txn_type="transfer_in",
        channel="bank_transfer",
        status="completed",
        scenario_id=sid,
        typology="mule_passthrough",
    )
    n = int(rng.integers(2, 5))
    shares = rng.dirichlet(np.ones(n)) * float(rng.uniform(0.85, 0.98))
    ts = t0
    for share in shares:
        ts += timedelta(hours=float(rng.uniform(0.3, 6)))
        sink.emit(
            rng,
            cust,
            ts,
            inflow * float(share),
            "money_transfer",
            txn_type="transfer_out",
            status="completed",
            scenario_id=sid,
            typology="mule_passthrough",
        )


def low_and_slow(
    rng: np.random.Generator, cust: Customer, t0: datetime, sid: str, sink: Sink
) -> None:
    # Small, familiar-looking online purchases every few days for weeks.
    top = np.argsort(cust.prefs)[::-1][:4]
    ts = t0
    for _ in range(int(rng.integers(6, 14))):
        ts += timedelta(days=float(rng.uniform(1, 3)), hours=float(rng.uniform(-3, 3)))
        category = SPEND_CATEGORIES[int(top[int(rng.integers(len(top)))])]
        sink.emit(
            rng,
            cust,
            ts,
            sample_amount(rng, cust, category) * float(rng.uniform(0.5, 1.5)),
            category,
            channel="online",
            status="completed",
            scenario_id=sid,
            typology="low_and_slow",
        )


TYPOLOGIES: dict[str, Episode] = {
    "account_takeover": account_takeover,
    "card_testing": card_testing,
    "impossible_travel": impossible_travel,
    "high_risk_category_shift": high_risk_category_shift,
    "night_drain": night_drain,
    "mule_passthrough": mule_passthrough,
    "low_and_slow": low_and_slow,
}
