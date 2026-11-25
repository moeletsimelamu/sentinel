"""Exploratory checks used by the EDA notebook (and tested on their own).

The single-feature leakage check exists because we wrote both the generator and the
detectors: a feature that separates fraud almost perfectly on its own usually means the
generator left a fingerprint, not that the feature is brilliant.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sentinel.features import MODEL_FEATURES

from sentinel_ml.datasets.features import fit_reference, model_matrix

SUSPICIOUS_AUC = 0.95


@dataclass(frozen=True)
class Loaded:
    transactions: pd.DataFrame
    labels: pd.DataFrame
    splits: pd.DataFrame
    features: pd.DataFrame
    customers: pd.DataFrame


def load(folder: Path) -> Loaded:
    return Loaded(
        transactions=pd.read_parquet(folder / "transactions.parquet"),
        labels=pd.read_parquet(folder / "labels.parquet"),
        splits=pd.read_parquet(folder / "splits.parquet"),
        features=pd.read_parquet(folder / "features.parquet"),
        customers=pd.read_parquet(folder / "customers.parquet"),
    )


def auc(scores: pd.Series, labels: pd.Series) -> float:
    """ROC AUC via the Mann-Whitney U statistic (ties get average ranks)."""
    y = labels.astype(bool).to_numpy()
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = pd.Series(scores.to_numpy()).rank(method="average").to_numpy()
    return float((ranks[y].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def single_feature_auc(X: pd.DataFrame, y: pd.Series) -> pd.DataFrame:
    """Separability of each feature on its own. AUC is direction-free (max(a, 1 - a))."""
    rows = []
    for col in X.columns:
        a = auc(X[col], y)
        rows.append({"feature": col, "auc": a, "separability": max(a, 1 - a)})
    out = pd.DataFrame(rows).sort_values("separability", ascending=False, ignore_index=True)
    out["suspicious"] = out["separability"] >= SUSPICIOUS_AUC
    return out


def amount_outliers(transactions: pd.DataFrame, k: float = 3.0) -> pd.Series:
    """Flag amounts outside Tukey fences (k x IQR on log amount) within each category."""
    log_amount = np.log1p(transactions["amount"].astype(float))
    grouped = log_amount.groupby(transactions["merchant_category"])
    q1, q3 = grouped.transform("quantile", 0.25), grouped.transform("quantile", 0.75)
    iqr = q3 - q1
    return (log_amount < q1 - k * iqr) | (log_amount > q3 + k * iqr)


def training_matrix(data: Loaded) -> tuple[pd.DataFrame, pd.Series]:
    """Model inputs and observed labels for the training split, reference fitted on train."""
    train_ids = set(data.splits.loc[data.splits["split"] == "train", "external_id"])
    feats = data.features[data.features["external_id"].isin(train_ids)]
    X = model_matrix(feats, fit_reference(feats))
    y = data.labels.set_index("external_id").loc[X.index, "is_fraud_labelled"]
    if list(X.columns) != list(MODEL_FEATURES):
        raise ValueError("model matrix columns do not match MODEL_FEATURES")
    return X, y
