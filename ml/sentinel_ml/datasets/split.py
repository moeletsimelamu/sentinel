"""Leakage-safe dataset splits.

* **Time-based:** train = first 60% of the timeline, validation = next 20%, test = rest.
* **Episodes stay whole:** a fraud episode belongs to the split containing its first
  transaction. Any of its transactions that fall past that split's end are marked
  ``excluded`` rather than leaking a partial episode into a later split.
* **Customer-disjoint holdout:** 10% of customers (chosen with the dataset seed) are
  excluded from training entirely and used to check that models generalise beyond
  memorised customers.

Features are still computed over each customer's full history, which is legitimate:
history is always strictly in the past of the transaction being described.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from sentinel_ml.generate.profiles import Profile

SPLITS = ("train", "validation", "test", "holdout", "excluded")


@dataclass(frozen=True)
class SplitBoundaries:
    start: datetime
    train_end: datetime
    validation_end: datetime
    end: datetime

    def as_dict(self) -> dict[str, str]:
        return {k: v.isoformat() for k, v in self.__dict__.items()}


def boundaries(profile: Profile) -> SplitBoundaries:
    start = datetime.combine(profile.start, datetime.min.time(), tzinfo=UTC)
    days = profile.days
    return SplitBoundaries(
        start=start,
        train_end=start + timedelta(days=days * profile.split["train"]),
        validation_end=start
        + timedelta(days=days * (profile.split["train"] + profile.split["validation"])),
        end=start + timedelta(days=days),
    )


def holdout_customers(customer_refs: pd.Series, profile: Profile, seed: int) -> set[str]:
    refs = sorted(set(customer_refs))
    rng = np.random.default_rng(np.random.SeedSequence([seed, 0x5E1]))
    n = round(len(refs) * profile.holdout_customer_share)
    picked = rng.choice(len(refs), size=n, replace=False)
    return {refs[int(i)] for i in picked}


def _time_split(ts: pd.Series, b: SplitBoundaries) -> pd.Series:
    out = pd.Series("test", index=ts.index, dtype="object")
    out[ts < b.validation_end] = "validation"
    out[ts < b.train_end] = "train"
    return out


def assign_splits(
    transactions: pd.DataFrame, labels: pd.DataFrame, profile: Profile, seed: int
) -> tuple[pd.Series, SplitBoundaries, set[str]]:
    """Return the split of every transaction (aligned to ``transactions``)."""
    b = boundaries(profile)
    split = _time_split(transactions["occurred_at"], b)

    scenario = labels["scenario_id"]
    has_episode = scenario.notna()
    first_seen = transactions.loc[has_episode].groupby(scenario[has_episode])["occurred_at"].min()
    episode_split = _time_split(first_seen, b)
    owning = scenario[has_episode].map(episode_split)
    crosses = owning != split[has_episode]
    split.loc[owning.index[crosses]] = "excluded"

    holdout = holdout_customers(transactions["customer_ref"], profile, seed)
    split[transactions["customer_ref"].isin(holdout)] = "holdout"
    return split, b, holdout
