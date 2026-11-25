"""Read-only model registry API. Registration is a CLI operation (no artefact uploads over
HTTP); activation arrives with model-based scoring in Phase 7."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from sentinel.core.db import get_db
from sentinel.core.permissions import Permission
from sentinel.modules.auth.dependencies import RequirePermission
from sentinel.modules.models import service
from sentinel.modules.models.models import ModelVersion
from sentinel.modules.models.schemas import EvaluationOut, ModelDetail, ModelSummary

router = APIRouter(prefix="/models", tags=["models"])
_read = RequirePermission(Permission.MODELS_READ)


def _headline(model: ModelVersion) -> dict[str, float | None]:
    test = next(
        (e for e in model.evaluations if e.split == "test" and e.threshold_kind == "budget"), None
    )
    m = test.metrics if test else {}
    return {
        "test_pr_auc": m.get("pr_auc"),
        "test_recall_at_budget": m.get("recall"),
        "test_precision_at_budget": m.get("precision"),
        "test_alerts_per_1000": m.get("alerts_per_1000"),
    }


def _summary(model: ModelVersion) -> ModelSummary:
    return ModelSummary(
        id=model.id,
        name=model.name,
        version=model.version,
        family=model.family,
        status=model.status,
        training_dataset_version=model.training_dataset_version,
        selected_by_experiment=model.selected_by_experiment,
        registered_at=model.registered_at,
        **_headline(model),
    )


@router.get("", response_model=list[ModelSummary], dependencies=[Depends(_read)])
def list_models(db: Session = Depends(get_db)) -> list[ModelSummary]:
    """All registered models — compare them side by side on the same test split."""
    return [_summary(m) for m in service.list_models(db)]


@router.get("/{model_id}", response_model=ModelDetail, dependencies=[Depends(_read)])
def get_model(model_id: uuid.UUID, db: Session = Depends(get_db)) -> ModelDetail:
    model = service.get_model(db, model_id)
    m = model.manifest
    return ModelDetail(
        **_summary(model).model_dump(),
        feature_set_version=model.feature_set_version,
        feature_schema_hash=model.feature_schema_hash,
        artefact_sha256=model.artefact_sha256,
        calibration=m.get("calibration"),
        thresholds=m.get("thresholds") or {},
        params=m.get("params") or m.get("rule_params"),
        libraries=m.get("libraries"),
        recommended_scoring=m.get("recommended_scoring"),
        recommended_rule_changes=m.get("recommended_changes"),
        evaluations=[EvaluationOut.model_validate(e) for e in model.evaluations],
    )
