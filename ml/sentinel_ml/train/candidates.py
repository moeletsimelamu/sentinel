"""Candidate models: logistic regression, random forest, isolation forest.

Protocol (docs/MODEL_EVALUATION.md §2):

* Hyperparameters: small documented grids, scored by PR-AUC with ``TimeSeriesSplit``
  (3 folds) **inside the training period only**.
* Calibration (supervised models): the validation split is cut in time; a sigmoid and an
  isotonic calibrator are fitted on the earlier half, and the one with the lower Brier
  score on the later half is kept. The model itself never sees validation labels.
* Isolation forest is fitted on training features without labels; its score is the share
  of training transactions that look *less* anomalous (a 0-1 rank, not a probability).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import IsolationForest, RandomForestClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from sentinel_ml.evaluate import metrics
from sentinel_ml.train.data import Split

N_JOBS = 2  # bounded: training must fit on a laptop alongside the app


@dataclass
class Fitted:
    family: str
    estimator: Any  # what gets persisted
    params: dict[str, Any]
    seed: int
    calibration: str | None = None
    tuning: list[dict[str, Any]] = field(default_factory=list)
    train_scores: np.ndarray | None = None  # isolation forest rank reference

    def score(self, X: pd.DataFrame) -> np.ndarray:
        if self.family == "isolation_forest":
            assert self.train_scores is not None  # noqa: S101
            raw = -self.estimator.score_samples(X)
            return np.searchsorted(self.train_scores, raw, side="right") / len(self.train_scores)
        return self.estimator.predict_proba(X)[:, 1]


def logistic_regression(params: dict[str, Any], seed: int) -> Pipeline:
    return Pipeline(
        [
            ("scale", StandardScaler()),
            (
                "model",
                LogisticRegression(
                    C=params["C"], class_weight="balanced", max_iter=5000, random_state=seed
                ),
            ),
        ]
    )


def random_forest(params: dict[str, Any], seed: int) -> RandomForestClassifier:
    return RandomForestClassifier(
        n_estimators=150,
        max_depth=params["max_depth"],
        min_samples_leaf=params["min_samples_leaf"],
        max_features="sqrt",
        class_weight="balanced_subsample",
        n_jobs=N_JOBS,
        random_state=seed,
    )


GRIDS: dict[str, dict[str, list[Any]]] = {
    "logreg": {"C": [0.01, 0.1, 1.0]},
    "random_forest": {"max_depth": [8, 16], "min_samples_leaf": [5, 25]},
}
BUILDERS = {"logreg": logistic_regression, "random_forest": random_forest}


def tune(family: str, train: Split, seed: int = 0) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    grid = GRIDS[family]
    folds = list(TimeSeriesSplit(n_splits=3).split(train.X))  # train is time-ordered
    log = []
    best: tuple[float, dict[str, Any]] | None = None
    for values in itertools.product(*grid.values()):
        params = dict(zip(grid, values, strict=True))
        scores = []
        for fit_idx, eval_idx in folds:
            if not train.y[fit_idx].any() or not train.y[eval_idx].any():
                continue
            model = BUILDERS[family](params, seed).fit(train.X.iloc[fit_idx], train.y[fit_idx])
            p = model.predict_proba(train.X.iloc[eval_idx])[:, 1]
            scores.append(metrics.average_precision(p, train.y[eval_idx]))
        if not scores:  # no fold had positives on both sides
            continue
        mean = float(np.mean(scores))
        log.append({"params": params, "cv_pr_auc_mean": mean, "cv_pr_auc_folds": scores})
        if best is None or mean > best[0]:
            best = (mean, params)
    if best is None:
        raise ValueError(f"No time fold has positives for tuning {family}")
    return best[1], log


def _calibrate(model: Any, validation: Split) -> tuple[Any, str]:
    half = len(validation) // 2
    X_fit, y_fit = validation.X.iloc[:half], validation.y[:half]
    X_sel, y_sel = validation.X.iloc[half:], validation.y[half:]
    best: tuple[float, Any, str] | None = None
    for method in ("sigmoid", "isotonic"):
        cal = CalibratedClassifierCV(FrozenEstimator(model), method=method).fit(X_fit, y_fit)
        b = metrics.brier(cal.predict_proba(X_sel)[:, 1], y_sel)
        if best is None or b < best[0]:
            best = (b, cal, method)
    assert best is not None  # noqa: S101
    return best[1], best[2]


def fit_supervised(
    family: str, params: dict[str, Any], train: Split, validation: Split, seed: int
) -> Fitted:
    model = BUILDERS[family](params, seed).fit(train.X, train.y)
    calibrated, method = _calibrate(model, validation)
    return Fitted(family, calibrated, params, seed, calibration=method)


def fit_isolation_forest(train: Split, seed: int) -> Fitted:
    params = {"n_estimators": 200, "max_samples": min(4096, len(train))}
    model = IsolationForest(**params, random_state=seed, n_jobs=N_JOBS).fit(train.X)
    reference = np.sort(-model.score_samples(train.X))
    return Fitted("isolation_forest", model, params, seed, train_scores=reference)


def refit_like(fitted: Fitted, train: Split, validation: Split, seed: int) -> Fitted:
    """Same configuration, different seed (for the seed-variance report)."""
    if fitted.family == "isolation_forest":
        return fit_isolation_forest(train, seed)
    return fit_supervised(fitted.family, fitted.params, train, validation, seed)
