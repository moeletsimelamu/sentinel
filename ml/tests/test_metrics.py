"""Metric functions on hand-computed cases, plus cross-checks against scikit-learn."""

from __future__ import annotations

import numpy as np
import pytest
from sklearn.metrics import average_precision_score, roc_auc_score

from sentinel_ml.evaluate import metrics


def test_confusion_and_rates() -> None:
    c = metrics.confusion([0.9, 0.8, 0.3, 0.2], [1, 0, 1, 0], 0.5)
    assert (c.tp, c.fp, c.tn, c.fn) == (1, 1, 1, 1)
    assert c.precision == c.recall == c.f1 == c.fpr == 0.5
    assert c.alerts_per_1000 == 500 and c.fp_per_1000 == 250


def test_threshold_is_inclusive() -> None:
    assert metrics.confusion([0.5], [1], 0.5).tp == 1


def test_empty_denominators_are_zero_not_errors() -> None:
    c = metrics.confusion([0.1, 0.2], [0, 0], 0.9)
    assert c.precision == 0 and c.recall == 0 and c.f1 == 0


def test_average_precision_hand_computed() -> None:
    # ranks: (P=1, R=.5), (P=.5, R=.5), (P=2/3, R=1) -> .5*1 + .5*2/3
    assert metrics.average_precision([0.9, 0.8, 0.7, 0.6], [1, 0, 1, 0]) == pytest.approx(5 / 6)


def test_average_precision_with_ties() -> None:
    # tie group {0.5, 0.5} counted together: (P=.5, R=.5) then (P=2/3, R=1)
    assert metrics.average_precision([0.5, 0.5, 0.1], [1, 0, 1]) == pytest.approx(
        0.5 * 0.5 + 0.5 * 2 / 3
    )


def test_roc_auc_hand_computed() -> None:
    assert metrics.roc_auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == pytest.approx(0.75)
    assert metrics.roc_auc([0.5, 0.5], [0, 1]) == pytest.approx(0.5)


def test_undefined_metrics_are_nan() -> None:
    assert np.isnan(metrics.average_precision([0.1, 0.2], [0, 0]))
    assert np.isnan(metrics.roc_auc([0.1, 0.2], [1, 1]))


def test_matches_scikit_learn_on_random_data() -> None:
    rng = np.random.default_rng(0)
    y = rng.random(5000) < 0.02
    s = rng.random(5000) + y * 0.3
    s[:500] = np.round(s[:500], 1)  # include ties
    assert metrics.average_precision(s, y) == pytest.approx(average_precision_score(y, s))
    assert metrics.roc_auc(s, y) == pytest.approx(roc_auc_score(y, s))


def test_brier() -> None:
    assert metrics.brier([1.0, 0.0, 0.5], [1, 0, 1]) == pytest.approx(0.25 / 3)


def test_budget_threshold_distinct_scores() -> None:
    s = np.arange(1000, dtype=float)
    thr = metrics.budget_threshold(s, 10)
    assert thr == 990 and int((s >= thr).sum()) == 10


def test_budget_threshold_never_exceeds_budget_on_ties() -> None:
    s = [5, 4, 4, 4, 1]
    thr = metrics.budget_threshold(s, 400)  # 2 alerts allowed; 4 is tied 3 ways
    assert thr == 5 and sum(x >= thr for x in s) == 1


def test_budget_threshold_edges() -> None:
    assert metrics.budget_threshold([1, 2, 3], 1) == float("inf")  # budget rounds to 0
    assert metrics.budget_threshold([1, 2, 3], 1000) == 1


def test_f1_optimal_threshold() -> None:
    assert metrics.f1_optimal_threshold([0.9, 0.8, 0.7, 0.1], [1, 1, 0, 0]) == 0.8


def test_value_detection_rate() -> None:
    vdr = metrics.value_detection_rate([0.9, 0.1, 0.9], [1, 1, 0], [100, 1000, 50], 0.5)
    assert vdr == pytest.approx(100 / 1100)


def test_group_recall() -> None:
    out = metrics.group_recall([0.9, 0.1, 0.8, 0.2], [1, 1, 1, 0], ["a", "a", "b", ""], 0.5)
    assert out == {
        "a": {"positives": 2, "caught": 1, "recall": 0.5},
        "b": {"positives": 1, "caught": 1, "recall": 1.0},
    }


def test_bootstrap_interval_brackets_point_estimate() -> None:
    rng = np.random.default_rng(1)
    y = rng.random(3000) < 0.05
    s = rng.random(3000) + y * 0.5
    ci = metrics.bootstrap_ci(s, y, 0.9, resamples=200, seed=0)
    ap = metrics.average_precision(s, y)
    assert ci["pr_auc"][0] <= ap <= ci["pr_auc"][1]
    assert 0 <= ci["recall"][0] <= ci["recall"][1] <= 1


def test_curves_shape() -> None:
    rng = np.random.default_rng(2)
    y = rng.random(1000) < 0.1
    c = metrics.curves(rng.random(1000), y, points=50)
    assert len(c["pr"]) <= 50 and c["roc"][0] == [0.0, 0.0]
    assert sum(c["histogram"]["positive"]) == y.sum()
    assert {b["bin"] for b in c["reliability"]} <= {i / 10 for i in range(10)}


def test_shape_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        metrics.confusion([0.1, 0.2], [1], 0.5)
