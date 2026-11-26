from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


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
    contributions: dict[str, Any] | None = None
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


class Band(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(pattern=r"^[a-z]{2,16}$")]
    min_score: int = Field(ge=0, le=100)


class ScoringConfigChange(BaseModel):
    """A new scoring configuration. ``w_model`` is derived as ``1 - w_rule``."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    w_rule: float = Field(ge=0, le=1)
    rule_cap: int = Field(ge=1, le=100)
    bands: list[Band] = Field(min_length=2, max_length=6)
    alert_threshold: int = Field(ge=1, le=100)
    change_reason: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=10, max_length=500)
    ]

    @model_validator(mode="after")
    def _bands_valid(self) -> ScoringConfigChange:
        mins = [b.min_score for b in self.bands]
        if mins[0] != 0 or mins != sorted(set(mins)):
            raise ValueError("bands must start at 0 and strictly increase")
        if len({b.name for b in self.bands}) != len(self.bands):
            raise ValueError("band names must be unique")
        return self
