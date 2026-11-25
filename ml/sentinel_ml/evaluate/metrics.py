"""Evaluation metrics for rare-class scoring.

Implemented directly (numpy) so each is small enough to verify on hand-computed cases;
the tests also cross-check ranking metrics against scikit-learn.

Accuracy is deliberately absent: at ~1% prevalence "never fraud" is 99% accurate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np
from scipy.stats import rankdata

DEFAULT_BUDGET_PER_1000 = 10.0


def _arrays(scores: Any, y: Any) -> tuple[np.ndarray, np.ndarray]:
    s = np.asarray(scores, dtype=float)
    t = np.asarray(y, dtype=bool)
    if s.shape != t.shape:
        raise ValueError("scores and labels must have the same shape")
    return s, t


@dataclass(frozen=True)
class Confusion:
    tp: int
    fp: int
    tn: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def fpr(self) -> float:
        return self.fp / (self.fp + self.tn) if self.fp + self.tn else 0.0

    @property
    def alerts_per_1000(self) -> float:
        n = self.tp + self.fp + self.tn + self.fn
        return 1000 * (self.tp + self.fp) / n if n else 0.0

    @property
    def fp_per_1000(self) -> float:
        n = self.tp + self.fp + self.tn + self.fn
        return 1000 * self.fp / n if n else 0.0

    def as_dict(self) -> dict[str, float]:
        return {
            **asdict(self),
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "fpr": self.fpr,
            "alerts_per_1000": self.alerts_per_1000,
            "fp_per_1000": self.fp_per_1000,
        }


def confusion(scores: Any, y: Any, threshold: float) -> Confusion:
    """Alert when ``score >= threshold``."""
    s, t = _arrays(scores, y)
    pred = s >= threshold
    return Confusion(
        tp=int((pred & t).sum()),
        fp=int((pred & ~t).sum()),
        tn=int((~pred & ~t).sum()),
        fn=int((~pred & t).sum()),
    )


def average_precision(scores: Any, y: Any) -> float:
    """PR-AUC as average precision: Σ (R_k - R_{k-1}) · P_k over distinct thresholds."""
    s, t = _arrays(scores, y)
    n_pos = int(t.sum())
    if n_pos == 0:
        return float("nan")
    order = np.argsort(-s, kind="mergesort")
    s, t = s[order], t[order]
    tp = np.cumsum(t)
    fp = np.cumsum(~t)
    last = np.r_[np.nonzero(np.diff(s))[0], len(s) - 1]  # end of each tie group
    tp, fp = tp[last], fp[last]
    precision = tp / (tp + fp)
    recall = tp / n_pos
    return float(np.sum(np.diff(np.r_[0.0, recall]) * precision))


def roc_auc(scores: Any, y: Any) -> float:
    """Probability a random positive outranks a random negative (ties count half)."""
    s, t = _arrays(scores, y)
    n_pos, n_neg = int(t.sum()), int((~t).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = rankdata(s)  # average ranks for ties
    return float((ranks[t].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def brier(probabilities: Any, y: Any) -> float:
    p, t = _arrays(probabilities, y)
    return float(np.mean((p - t.astype(float)) ** 2))


def budget_threshold(scores: Any, budget_per_1000: float = DEFAULT_BUDGET_PER_1000) -> float:
    """Lowest threshold that alerts on **at most** ``budget_per_1000`` per 1,000 rows.

    With tied scores at the cut-off the whole tie group is excluded rather than
    exceeding the budget.
    """
    s = np.sort(np.asarray(scores, dtype=float))[::-1]
    k = int(np.floor(len(s) * budget_per_1000 / 1000))
    if k <= 0:
        return float("inf")
    if k >= len(s):
        return float(s[-1])
    cut = s[k - 1]
    if s[k] < cut:
        return float(cut)
    higher = s[s > cut]  # tie spans the cut-off: alert only on strictly higher scores
    return float(higher.min()) if len(higher) else float("inf")


def f1_optimal_threshold(scores: Any, y: Any) -> float:
    """Threshold maximising F1 (evaluated at every distinct score, vectorised)."""
    s, t = _arrays(scores, y)
    n_pos = int(t.sum())
    if n_pos == 0:
        return float("inf")
    order = np.argsort(-s, kind="mergesort")
    s, t = s[order], t[order]
    last = np.r_[np.nonzero(np.diff(s))[0], len(s) - 1]
    tp = np.cumsum(t)[last]
    fp = np.cumsum(~t)[last]
    f1 = 2 * tp / (2 * tp + fp + (n_pos - tp))
    return float(s[last][int(np.argmax(f1))])


def value_detection_rate(scores: Any, y: Any, amounts: Any, threshold: float) -> float:
    """Share of fraudulent *amount* that is alerted at ``threshold``."""
    s, t = _arrays(scores, y)
    a = np.asarray(amounts, dtype=float)
    total = a[t].sum()
    return float(a[t & (s >= threshold)].sum() / total) if total else float("nan")


def group_recall(scores: Any, y: Any, groups: Any, threshold: float) -> dict[str, dict[str, float]]:
    """Recall per group (e.g. typology), over positives only."""
    s, t = _arrays(scores, y)
    g = np.asarray(groups, dtype=object)
    out: dict[str, dict[str, float]] = {}
    for name in sorted({str(v) for v in g[t]}):
        mask = t & (g.astype(str) == name)
        caught = int((s[mask] >= threshold).sum())
        out[name] = {
            "positives": int(mask.sum()),
            "caught": caught,
            "recall": caught / int(mask.sum()),
        }
    return out


def _downsample(x: np.ndarray, y: np.ndarray, points: int) -> list[list[float]]:
    if len(x) > points:
        idx = np.unique(np.linspace(0, len(x) - 1, points).round().astype(int))
        x, y = x[idx], y[idx]
    return [[round(float(a), 5), round(float(b), 5)] for a, b in zip(x, y, strict=True)]


def curves(scores: Any, y: Any, points: int = 100) -> dict[str, Any]:
    """Downsampled PR and ROC curves, reliability (for 0-1 scores) and class histograms."""
    s, t = _arrays(scores, y)
    order = np.argsort(-s, kind="mergesort")
    tp = np.cumsum(t[order])
    fp = np.cumsum(~t[order])
    n_pos, n_neg = max(int(t.sum()), 1), max(int((~t).sum()), 1)
    precision = tp / np.maximum(tp + fp, 1)
    recall = tp / n_pos
    out: dict[str, Any] = {
        "pr": _downsample(recall, precision, points),
        "roc": _downsample(np.r_[0.0, fp / n_neg], np.r_[0.0, recall], points),
    }
    lo, hi = float(s.min()), float(s.max())
    edges = np.linspace(lo, hi if hi > lo else lo + 1, 21)
    out["histogram"] = {
        "edges": [round(float(e), 5) for e in edges],
        "positive": np.histogram(s[t], edges)[0].tolist(),
        "negative": np.histogram(s[~t], edges)[0].tolist(),
    }
    if lo >= 0 and hi <= 1:
        bins = np.clip((s * 10).astype(int), 0, 9)
        out["reliability"] = [
            {
                "bin": b / 10,
                "count": int((bins == b).sum()),
                "mean_score": float(s[bins == b].mean()),
                "observed_rate": float(t[bins == b].mean()),
            }
            for b in range(10)
            if (bins == b).any()
        ]
    return out


def bootstrap_ci(
    scores: Any,
    y: Any,
    threshold: float,
    *,
    resamples: int = 1000,
    seed: int = 0,
) -> dict[str, list[float]]:
    """95% percentile intervals for PR-AUC and recall at ``threshold``."""
    s, t = _arrays(scores, y)
    rng = np.random.default_rng(seed)
    n = len(s)
    ap, rec = [], []
    for _ in range(resamples):
        idx = rng.integers(0, n, n)
        if not t[idx].any():
            continue
        ap.append(average_precision(s[idx], t[idx]))
        rec.append(confusion(s[idx], t[idx], threshold).recall)
    return {
        "pr_auc": [float(np.percentile(ap, 2.5)), float(np.percentile(ap, 97.5))],
        "recall": [float(np.percentile(rec, 2.5)), float(np.percentile(rec, 97.5))],
    }


def evaluate(
    scores: Any,
    y: Any,
    *,
    threshold: float,
    amounts: Any | None = None,
    probabilities: bool = False,
) -> dict[str, Any]:
    s, t = _arrays(scores, y)
    c = confusion(s, t, threshold)
    out: dict[str, Any] = {
        "n": len(s),
        "positives": int(t.sum()),
        "prevalence": float(t.mean()) if len(t) else float("nan"),
        "pr_auc": average_precision(s, t),
        "roc_auc": roc_auc(s, t),
        "threshold": threshold,
        **c.as_dict(),
    }
    if amounts is not None:
        out["value_detection_rate"] = value_detection_rate(s, t, amounts, threshold)
    if probabilities:
        out["brier"] = brier(s, t)
    return out
