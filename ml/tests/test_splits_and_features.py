"""Splits, offline features, leakage controls and the model-input schema."""

from __future__ import annotations

import math

import pandas as pd
import pytest
from sentinel.features import FORBIDDEN_INPUTS, MODEL_FEATURES

from sentinel_ml.datasets import eda
from sentinel_ml.datasets.build import BuiltDataset
from sentinel_ml.datasets.features import (
    build_feature_table,
    feature_rows,
    fit_reference,
    model_matrix,
)
from sentinel_ml.datasets.split import assign_splits, boundaries
from sentinel_ml.generate.generator import GeneratedDataset


@pytest.fixture(scope="module")
def split_frame(tiny: GeneratedDataset) -> pd.DataFrame:
    splits, _, _ = assign_splits(tiny.transactions, tiny.labels, tiny.profile, tiny.seed)
    return tiny.transactions.assign(split=splits.to_numpy()).merge(tiny.labels, on="external_id")


# ------------------------------------------------------------------ splits


def test_time_split_is_ordered(tiny: GeneratedDataset, split_frame: pd.DataFrame) -> None:
    b = boundaries(tiny.profile)
    by_split = split_frame.groupby("split")["occurred_at"]
    assert by_split.max()["train"] < b.train_end <= by_split.min()["validation"]
    assert by_split.max()["validation"] < b.validation_end <= by_split.min()["test"]


def test_holdout_customers_are_disjoint(tiny: GeneratedDataset, split_frame: pd.DataFrame) -> None:
    holdout = set(split_frame.loc[split_frame["split"] == "holdout", "customer_ref"])
    others = set(split_frame.loc[split_frame["split"] != "holdout", "customer_ref"])
    assert holdout and not holdout & others
    share = len(holdout) / split_frame["customer_ref"].nunique()
    assert share == pytest.approx(tiny.profile.holdout_customer_share, abs=0.02)


def test_holdout_is_deterministic(tiny: GeneratedDataset) -> None:
    a, _, h1 = assign_splits(tiny.transactions, tiny.labels, tiny.profile, tiny.seed)
    b, _, h2 = assign_splits(tiny.transactions, tiny.labels, tiny.profile, tiny.seed)
    assert h1 == h2 and a.equals(b)


def test_episodes_never_span_splits(split_frame: pd.DataFrame) -> None:
    episodes = split_frame[
        split_frame["scenario_id"].notna() & (split_frame["split"] != "excluded")
    ]
    assert (episodes.groupby("scenario_id")["split"].nunique() == 1).all()
    excluded = split_frame[split_frame["split"] == "excluded"]
    assert excluded["is_fraud_injected"].all()  # only episode continuations are excluded


# ---------------------------------------------------------------- features


def test_feature_table_has_one_row_per_transaction(built: BuiltDataset) -> None:
    t = pd.read_parquet(built.path / "transactions.parquet")
    f = pd.read_parquet(built.path / "features.parquet")
    assert list(f["external_id"]) == list(t["external_id"])


def test_features_contain_no_labels_or_identifiers(built: BuiltDataset) -> None:
    f = pd.read_parquet(built.path / "features.parquet")
    assert not (set(f.columns) - {"external_id"}) & FORBIDDEN_INPUTS
    assert not set(MODEL_FEATURES) & FORBIDDEN_INPUTS
    reference = fit_reference(f)
    X = model_matrix(f.head(200), reference)
    assert list(X.columns) == list(MODEL_FEATURES)
    assert all(math.isfinite(v) for v in X.to_numpy().ravel())


def test_no_future_leakage_across_the_split_boundary(tiny: GeneratedDataset) -> None:
    """Training-period features must be identical whether or not later data exists."""
    b = boundaries(tiny.profile)
    t = tiny.transactions
    train_only = t[t["occurred_at"] < b.train_end]
    with_future = build_feature_table(t).set_index("external_id").loc[train_only["external_id"]]
    without = build_feature_table(train_only).set_index("external_id")
    pdt_cols = [c for c in without.columns]
    pd.testing.assert_frame_equal(with_future[pdt_cols], without[pdt_cols])


def test_reference_is_fitted_on_training_split_only(built: BuiltDataset) -> None:
    import json

    f = pd.read_parquet(built.path / "features.parquet")
    s = pd.read_parquet(built.path / "splits.parquet")
    stored = json.loads((built.path / "feature_reference.json").read_text(encoding="utf-8"))
    train = f[(s["split"] == "train").to_numpy()]
    assert stored == fit_reference(train).to_dict()
    assert stored != fit_reference(f).to_dict()


def test_feature_rows_match_direct_backend_call(tiny: GeneratedDataset) -> None:
    """The ml path is a thin adapter over sentinel.features — same numbers either way."""
    sample = tiny.transactions[tiny.transactions["customer_ref"] == "C-00010"]
    via_ml = dict(feature_rows(sample))
    table = build_feature_table(sample).set_index("external_id")
    for ext, row in via_ml.items():
        assert table.loc[ext, "history_count_90d"] == row.history_count_90d
        assert table.loc[ext, "amount_zar"] == pytest.approx(row.amount_zar)


# ---------------------------------------------------------------------- eda


def test_auc_on_hand_computed_cases() -> None:
    y = pd.Series([0, 0, 1, 1])
    assert eda.auc(pd.Series([0.1, 0.2, 0.8, 0.9]), y) == 1.0
    assert eda.auc(pd.Series([0.9, 0.8, 0.2, 0.1]), y) == 0.0
    assert eda.auc(pd.Series([0.5, 0.5, 0.5, 0.5]), y) == 0.5
    assert eda.auc(pd.Series([0.1, 0.4, 0.35, 0.8]), y) == 0.75


def test_no_single_feature_gives_the_answer_away(built: BuiltDataset) -> None:
    X, y = eda.training_matrix(eda.load(built.path))
    sep = eda.single_feature_auc(X, y)
    assert not sep["suspicious"].any(), sep.head()


def test_amount_outliers_flag_extremes() -> None:
    t = pd.DataFrame(
        {"amount": [100.0] * 20 + [100.0 * 10_000], "merchant_category": ["groceries"] * 21}
    )
    t.loc[0:9, "amount"] = 120.0
    flags = eda.amount_outliers(t)
    assert flags.iloc[-1] and not flags.iloc[:-1].any()
