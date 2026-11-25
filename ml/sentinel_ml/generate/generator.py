"""Deterministic dataset generation: ``generate(profile, seed)``.

Every random draw comes from a ``numpy.random.SeedSequence`` derived from ``seed`` (one
child stream per customer, one for fraud injection), so the same profile, seed and
generator version always produce identical data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from sentinel_ml import GENERATOR_VERSION
from sentinel_ml.generate.fraud import TYPOLOGIES
from sentinel_ml.generate.profiles import Profile
from sentinel_ml.generate.simulate import (
    LOCAL_UTC_OFFSET_H,
    Sink,
    make_customers,
    simulate_customer,
)

TRANSACTION_COLUMNS = (
    "external_id",
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
)
LABEL_COLUMNS = (
    "external_id",
    "is_fraud_injected",
    "is_fraud_labelled",
    "typology",
    "scenario_id",
    "benign_scenario",
)
MIN_HISTORY_DAYS = 14  # episodes start after customers have some history


@dataclass(frozen=True)
class GeneratedDataset:
    version: str
    profile: Profile
    seed: int
    transactions: pd.DataFrame  # ingestible columns only — no labels
    labels: pd.DataFrame  # evaluation only
    customers: pd.DataFrame  # generator metadata (segment etc.) — never ingested
    episodes: pd.DataFrame  # one row per injected episode


def dataset_version(profile: Profile, seed: int) -> str:
    return f"{profile.name}-{GENERATOR_VERSION}-{seed}"


def generate(profile: Profile, seed: int) -> GeneratedDataset:
    root = np.random.SeedSequence(seed)
    customer_seq, fraud_seq, *activity_seqs = root.spawn(2 + profile.n_customers)
    customers = make_customers(np.random.default_rng(customer_seq), profile)

    sink = Sink()
    for cust, seq in zip(customers, activity_seqs, strict=True):
        simulate_customer(np.random.default_rng(seq), cust, profile, sink)

    rng = np.random.default_rng(fraud_seq)
    start = datetime.combine(profile.start, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=profile.days)
    months = profile.days / 30
    n_episodes = max(
        len(TYPOLOGIES),
        round(profile.n_customers * months * profile.fraud_episodes_per_customer_month),
    )
    names = list(profile.typology_weights)
    weights = np.array([profile.typology_weights[n] for n in names])
    # Guarantee every typology appears at least once, then sample the rest by weight.
    chosen = names + [
        names[int(i)] for i in rng.choice(len(names), size=n_episodes - len(names), p=weights)
    ]
    episodes = []
    for k, typology in enumerate(chosen):
        victim = customers[int(rng.integers(len(customers)))]
        day = float(rng.uniform(MIN_HISTORY_DAYS, profile.days - 4))
        local_hour = float(rng.uniform(8, 21))
        t0 = start + timedelta(days=int(day), hours=local_hour - LOCAL_UTC_OFFSET_H)
        sid = f"EP-{k + 1:05d}"
        TYPOLOGIES[typology](rng, victim, t0, sid, sink)
        episodes.append(
            {
                "scenario_id": sid,
                "typology": typology,
                "customer_ref": victim.ref,
                "labelled": bool(rng.random() >= profile.label_noise),
            }
        )
    episode_df = pd.DataFrame(episodes)

    df = pd.DataFrame(sink.cols)
    df["occurred_at"] = pd.to_datetime(df["occurred_at"], utc=True).dt.floor("s")
    df = df[(df["occurred_at"] >= start) & (df["occurred_at"] < end)]
    df = df.sort_values(["occurred_at", "customer_ref"], kind="mergesort").reset_index(drop=True)
    df.insert(0, "external_id", [f"T{i + 1:08d}" for i in range(len(df))])

    labelled = set(episode_df.loc[episode_df["labelled"], "scenario_id"])
    injected = df["scenario_id"].notna()
    labels = pd.DataFrame(
        {
            "external_id": df["external_id"],
            "is_fraud_injected": injected,
            "is_fraud_labelled": injected & df["scenario_id"].isin(labelled),
            "typology": df["typology"],
            "scenario_id": df["scenario_id"],
            "benign_scenario": df["benign_scenario"],
        }
    )
    cust_df = pd.DataFrame(
        {
            "customer_ref": [c.ref for c in customers],
            "segment": [c.segment for c in customers],
            "home_city": [c.home.name for c in customers],
            "shift_worker": [c.shift_worker for c in customers],
            "trips": [len(c.trips) for c in customers],
            "new_phone": [c.new_phone is not None for c in customers],
        }
    )
    return GeneratedDataset(
        version=dataset_version(profile, seed),
        profile=profile,
        seed=seed,
        transactions=df[list(TRANSACTION_COLUMNS)].copy(),
        labels=labels,
        customers=cust_df,
        episodes=episode_df,
    )
