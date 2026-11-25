"""Evaluate a configured rule set against feature rows. Pure."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from sentinel.detection.rules import CATALOGUE, RuleParams, Severity
from sentinel.features.behaviour import FeatureRow


class UnknownRuleError(ValueError):
    pass


class InvalidRuleParamsError(ValueError):
    def __init__(self, code: str, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"Invalid parameters for rule {code}")
        self.code = code
        self.errors = errors


def validate_params(code: str, params: Mapping[str, Any]) -> RuleParams:
    rule = CATALOGUE.get(code)
    if rule is None:
        raise UnknownRuleError(code)
    try:
        return rule.params_schema.model_validate(dict(params))
    except ValidationError as exc:
        errors = [
            {"loc": list(e.get("loc", ())), "message": e.get("msg"), "type": e.get("type")}
            for e in exc.errors()
        ]
        raise InvalidRuleParamsError(code, errors) from None


def default_params(code: str) -> dict[str, Any]:
    return CATALOGUE[code].params_schema().model_dump()


@dataclass(frozen=True)
class ConfiguredRule:
    """One rule version as pinned by a rule set."""

    rule_version_id: uuid.UUID
    code: str
    params: RuleParams
    weight: int
    severity: Severity
    force_alert: bool
    is_active: bool


@dataclass(frozen=True)
class RuleHit:
    rule_version_id: uuid.UUID
    code: str
    weight: int
    severity: Severity
    force_alert: bool
    observed: dict[str, Any]
    threshold: dict[str, Any]
    explanation: str


def evaluate(rules: Sequence[ConfiguredRule], row: FeatureRow) -> list[RuleHit]:
    hits: list[RuleHit] = []
    for cfg in rules:
        if not cfg.is_active:
            continue
        obs = CATALOGUE[cfg.code].check(row, cfg.params)
        if obs is not None:
            hits.append(
                RuleHit(
                    rule_version_id=cfg.rule_version_id,
                    code=cfg.code,
                    weight=cfg.weight,
                    severity=cfg.severity,
                    force_alert=cfg.force_alert,
                    observed=obs.observed,
                    threshold=obs.threshold,
                    explanation=obs.explanation,
                )
            )
    return hits
