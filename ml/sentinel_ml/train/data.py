"""Load a built dataset into per-split arrays for training and evaluation.

Labels are joined here — and only here — by ``external_id``. Models train on the
*observed* label (``is_fraud_labelled``); evaluation also reports against the injected
ground truth to show the effect of undiscovered fraud.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sentinel.features import MODEL_FEATURES, FeatureReference, FeatureRow

from sentinel_ml.datasets.features import fit_reference, model_matrix, row_from_table

SPLIT_NAMES = ("train", "validation", "test", "holdout")


@dataclass
class Split:
    name: str
    features: pd.DataFrame  # raw FeatureRow columns (+ external_id)
    X: pd.DataFrame  # MODEL_FEATURES
    y: np.ndarray  # observed label
    y_true: np.ndarray  # injected ground truth
    amount_zar: np.ndarray
    typology: np.ndarray  # "" for legitimate
    benign: np.ndarray  # benign scenario or ""
    history: np.ndarray  # history_count_90d
    occurred_at: np.ndarray

    def __len__(self) -> int:
        return len(self.y)

    def rows(self) -> list[FeatureRow]:
        return [row_from_table(r) for r in self.features.to_dict(orient="records")]


@dataclass
class Dataset:
    version: str
    card: dict[str, Any]
    reference: FeatureReference
    splits: dict[str, Split]


def load_dataset(folder: Path) -> Dataset:
    card = json.loads((folder / "dataset_card.json").read_text(encoding="utf-8"))
    features = pd.read_parquet(folder / "features.parquet")
    labels = pd.read_parquet(folder / "labels.parquet")
    split_col = pd.read_parquet(folder / "splits.parquet")
    times = pd.read_parquet(folder / "transactions.parquet", columns=["external_id", "occurred_at"])
    frame = (
        features.merge(labels, on="external_id", validate="one_to_one")
        .merge(split_col, on="external_id", validate="one_to_one")
        .merge(times, on="external_id", validate="one_to_one")
        .sort_values(["occurred_at", "external_id"], kind="mergesort")
        .reset_index(drop=True)
    )
    train_feats = frame.loc[frame["split"] == "train", features.columns]
    reference = fit_reference(train_feats)  # training split only
    splits = {}
    for name in SPLIT_NAMES:
        part = frame[frame["split"] == name].reset_index(drop=True)
        feats = part[features.columns]
        X = model_matrix(feats, reference)
        if list(X.columns) != list(MODEL_FEATURES):
            raise ValueError("Feature schema mismatch")
        splits[name] = Split(
            name=name,
            features=feats,
            X=X.reset_index(drop=True),
            y=part["is_fraud_labelled"].to_numpy(dtype=bool),
            y_true=part["is_fraud_injected"].to_numpy(dtype=bool),
            amount_zar=part["amount_zar"].to_numpy(dtype=float),
            typology=part["typology"].fillna("").to_numpy(dtype=object),
            benign=part["benign_scenario"].fillna("").to_numpy(dtype=object),
            history=part["history_count_90d"].to_numpy(dtype=int),
            occurred_at=part["occurred_at"].to_numpy(),
        )
    return Dataset(card["dataset_version"], card, reference, splits)
