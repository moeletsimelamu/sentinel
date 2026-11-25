from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class RuleHitOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    rule_code: str
    rule_version_id: uuid.UUID
    weight_applied: int
    severity: str
    force_alert: bool
    observed: dict[str, Any]
    threshold: dict[str, Any]
    explanation: str


class AssessmentOut(BaseModel):
    """A risk assessment is an investigative indicator, not a finding of fraud."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    transaction_id: uuid.UUID
    mode: str
    risk_score: int
    risk_category: str
    rule_score: int
    model_score: float | None
    model_version_id: uuid.UUID | None
    force_alert: bool
    rule_set_id: uuid.UUID
    scoring_config_id: uuid.UUID
    feature_set_version: str
    features: dict[str, Any]
    reasons: list[str]
    hits: list[RuleHitOut]
    assessed_at: datetime


class DetectionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: uuid.UUID


class DetectionRunOut(BaseModel):
    id: uuid.UUID
    status: str
    batch_id: uuid.UUID
    rescore: bool
    attempts: int
    last_error: str | None
    result: dict[str, Any] | None
    created_at: datetime
    finished_at: datetime | None


class ScoringConfigOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version: int
    w_rule: float
    w_model: float
    rule_cap: int
    bands: list[dict[str, Any]]
    alert_threshold: int
    is_active: bool
    change_reason: str
    created_at: datetime
