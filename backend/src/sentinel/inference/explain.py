"""Per-feature contributions.

* **Logistic regression — exact.** In log-odds space the score is additive:
  ``coef_j * (x_j - mean_j) / scale_j`` per feature (relative to the training mean).
* **Random forest — path-based (Saabas).** Following each tree's decision path, the
  change in the node's fraud fraction at every split is credited to that split's feature;
  averaged over trees. Contributions plus the root value reproduce the forest's raw vote
  exactly, but the attribution is approximate (it depends on split order), and it
  explains the forest *before* probability calibration.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def linear_contributions(pipeline: Any, frame: pd.DataFrame) -> np.ndarray:
    scaler = pipeline.named_steps["scale"]
    model = pipeline.named_steps["model"]
    z = (frame.to_numpy(dtype=float) - scaler.mean_) / scaler.scale_
    return np.asarray(z * model.coef_[0], dtype=float)


def _fraud_fraction(tree: Any) -> np.ndarray:
    values = tree.tree_.value[:, 0, :]
    totals = values.sum(axis=1)
    return np.asarray(values[:, 1] / np.where(totals > 0, totals, 1), dtype=float)


def tree_contributions(forest: Any, frame: pd.DataFrame) -> np.ndarray:
    data = frame.to_numpy(dtype=np.float32)
    n, m = data.shape
    contrib = np.zeros((n, m))
    for est in forest.estimators_:
        p = _fraud_fraction(est)
        feature = est.tree_.feature
        paths = est.decision_path(data)
        for i in range(n):
            nodes = paths.indices[paths.indptr[i] : paths.indptr[i + 1]]
            nodes = np.sort(nodes)  # parents have smaller ids than their children
            parents, children = nodes[:-1], nodes[1:]
            np.add.at(contrib[i], feature[parents], p[children] - p[parents])
    return contrib / len(forest.estimators_)


def forest_bias(forest: Any) -> float:
    return float(np.mean([_fraud_fraction(est)[0] for est in forest.estimators_]))
