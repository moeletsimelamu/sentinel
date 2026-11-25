from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class EvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    split: str
    threshold_kind: str
    threshold: float | None
    dataset_version: str
    metrics: dict[str, Any]
    ci95: dict[str, Any] | None
    curves: dict[str, Any] | None


class ModelSummary(BaseModel):
    """Headline numbers are test metrics at the alert-budget threshold (synthetic data)."""

    id: uuid.UUID
    name: str
    version: str
    family: str
    status: str
    training_dataset_version: str
    selected_by_experiment: bool
    registered_at: datetime
    test_pr_auc: float | None
    test_recall_at_budget: float | None
    test_precision_at_budget: float | None
    test_alerts_per_1000: float | None


class ModelDetail(ModelSummary):
    feature_set_version: str
    feature_schema_hash: str
    artefact_sha256: str | None
    calibration: str | None
    thresholds: dict[str, Any]
    params: dict[str, Any] | None
    libraries: dict[str, Any] | None
    recommended_scoring: dict[str, Any] | None
    recommended_rule_changes: dict[str, Any] | None
    evaluations: list[EvaluationOut]
