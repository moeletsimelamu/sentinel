"""Candidate 0: the rule baseline, scored exactly as the application scores it.

Uses ``sentinel.detection`` (the same catalogue and engine as the worker). Rule scores
are capped weighted sums, so many transactions tie; the alert-budget threshold never
breaks ties arbitrarily, which is reported honestly as "fewer alerts than budget".

The noisiest rules are re-tuned on the **validation split only** (coordinate search over
a small, documented grid). The result is a recommended new rule version, not a silent
change: applying it in the app is an audited admin action.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sentinel.detection.engine import default_params, validate_params
from sentinel.detection.rules import CATALOGUE
from sentinel.features import FeatureRow

from sentinel_ml.evaluate import metrics

RULE_CAP = 100

# Parameters re-tuned on validation; every other rule keeps its v1 defaults.
TUNING_GRID: dict[str, dict[str, list[Any]]] = {
    "BEHAVIOUR_SHIFT": {"min_shift": [0.5, 0.6, 0.7, 0.8, 0.9]},
    "LARGE_AMOUNT_RELATIVE": {"min_multiple": [8.0, 12.0, 20.0, 40.0]},
    "UNUSUAL_HOUR": {"min_multiple": [2.0, 3.0, 5.0]},
    "VELOCITY_RATIO_24H": {"min_ratio": [5.0, 8.0, 12.0]},
}


@dataclass
class RuleConfig:
    params: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {code: default_params(code) for code in CATALOGUE}
    )
    weights: dict[str, int] = field(
        default_factory=lambda: {code: r.default_weight for code, r in CATALOGUE.items()}
    )

    def with_param(self, code: str, name: str, value: Any) -> RuleConfig:
        params = {c: dict(p) for c, p in self.params.items()}
        params[code][name] = value
        return RuleConfig(params, dict(self.weights))


def rule_hits(rows: Sequence[FeatureRow], code: str, params: Mapping[str, Any]) -> np.ndarray:
    rule = CATALOGUE[code]
    validated = validate_params(code, params)
    return np.fromiter((rule.check(r, validated) is not None for r in rows), bool, len(rows))


def hit_matrix(rows: Sequence[FeatureRow], config: RuleConfig) -> dict[str, np.ndarray]:
    return {code: rule_hits(rows, code, config.params[code]) for code in CATALOGUE}


def score(hits: Mapping[str, np.ndarray], config: RuleConfig) -> np.ndarray:
    total = sum(config.weights[c] * h.astype(float) for c, h in hits.items())
    return np.minimum(RULE_CAP, np.asarray(total, dtype=float))


def objective(scores: np.ndarray, y: np.ndarray, budget: float) -> tuple[float, float]:
    """Recall at the alert budget, then PR-AUC (the documented selection order)."""
    thr = metrics.budget_threshold(scores, budget)
    return (metrics.confusion(scores, y, thr).recall, metrics.average_precision(scores, y))


def tune(
    rows: Sequence[FeatureRow], y: np.ndarray, budget: float
) -> tuple[RuleConfig, list[dict[str, Any]]]:
    """Coordinate search on the validation rows; returns the config and a log."""
    config = RuleConfig()
    hits = hit_matrix(rows, config)
    log: list[dict[str, Any]] = []
    best = objective(score(hits, config), y, budget)
    for code, grid in TUNING_GRID.items():
        for name, values in grid.items():
            for value in values:
                candidate = config.with_param(code, name, value)
                trial = dict(hits)
                trial[code] = rule_hits(rows, code, candidate.params[code])
                obj = objective(score(trial, candidate), y, budget)
                log.append(
                    {
                        "rule": code,
                        "param": name,
                        "value": value,
                        "recall_at_budget": obj[0],
                        "pr_auc": obj[1],
                        "hit_rate": float(trial[code].mean()),
                    }
                )
                if obj > best:
                    best, config, hits = obj, candidate, trial
    return config, log


def changed_params(config: RuleConfig) -> dict[str, dict[str, Any]]:
    out = {}
    for code, params in config.params.items():
        defaults = default_params(code)
        diff = {k: v for k, v in params.items() if defaults.get(k) != v}
        if diff:
            out[code] = diff
    return out
