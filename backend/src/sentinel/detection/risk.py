"""Combine rule hits (and, from Phase 7, a model score) into a 0-100 risk score. Pure.

    rule_score  = min(rule_cap, Σ weight of triggered rules)
    risk_score  = rule_score                                    (rules-only mode)
                = round(w_rule x rule_score + w_model x 100 x model_score)   (combined)

In rules-only mode the rule component is used at full weight; otherwise a missing model
would cap every score at ``w_rule x 100`` and no transaction could reach the top band.

A risk score is an investigative indicator, not a finding.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from sentinel.detection.engine import RuleHit


class ScoringMode(StrEnum):
    RULES_ONLY = "rules_only"
    COMBINED = "combined"


@dataclass(frozen=True)
class Band:
    name: str
    min_score: int


@dataclass(frozen=True)
class ScoringSpec:
    w_rule: float
    w_model: float
    rule_cap: int
    bands: tuple[Band, ...]  # ascending by min_score, first must start at 0
    alert_threshold: int


@dataclass(frozen=True)
class RiskResult:
    mode: ScoringMode
    rule_score: int
    model_score: float | None
    risk_score: int
    category: str
    force_alert: bool
    reasons: tuple[str, ...]


def band_for(score: int, bands: Sequence[Band]) -> str:
    name = bands[0].name
    for band in bands:
        if score >= band.min_score:
            name = band.name
    return name


def combine(
    hits: Sequence[RuleHit], spec: ScoringSpec, model_score: float | None = None
) -> RiskResult:
    rule_score = min(spec.rule_cap, sum(h.weight for h in hits))
    if model_score is None:
        mode, risk = ScoringMode.RULES_ONLY, rule_score
    else:
        mode = ScoringMode.COMBINED
        risk = round(spec.w_rule * rule_score + spec.w_model * 100 * model_score)
    risk = max(0, min(100, risk))
    ordered = sorted(hits, key=lambda h: (-h.weight, h.code))
    return RiskResult(
        mode=mode,
        rule_score=rule_score,
        model_score=model_score,
        risk_score=risk,
        category=band_for(risk, spec.bands),
        force_alert=any(h.force_alert for h in hits),
        reasons=tuple(f"{h.explanation} (rule {h.code})" for h in ordered),
    )
