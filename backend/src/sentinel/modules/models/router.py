"""Model registry API. Registration is a CLI operation (no artefact uploads over HTTP);
activation and deactivation are audited admin actions (``models:activate``)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from sentinel.core.config import Settings, get_settings
from sentinel.core.db import get_db
from sentinel.core.permissions import Permission
from sentinel.modules.audit.context import RequestContext, get_request_context
from sentinel.modules.auth.dependencies import RequirePermission
from sentinel.modules.auth.schemas import Principal
from sentinel.modules.models import service
from sentinel.modules.models.models import ModelVersion
from sentinel.modules.models.schemas import (
    ActivationRequest,
    EvaluationOut,
    ModelDetail,
    ModelSummary,
)

router = APIRouter(prefix="/models", tags=["models"])
_read = RequirePermission(Permission.MODELS_READ)
_activate = RequirePermission(Permission.MODELS_ACTIVATE)


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
    return get_model_detail(service.get_model(db, model_id))


def get_model_detail(model: ModelVersion) -> ModelDetail:
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


@router.post("/{model_id}/activate", response_model=ModelDetail)
def activate_model(
    model_id: uuid.UUID,
    body: ActivationRequest,
    by: Principal = Depends(_activate),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> ModelDetail:
    """Use this model for scoring from the next batch on (hash and schema re-verified)."""
    return get_model_detail(service.activate(db, settings, model_id, body.reason, by, ctx))


@router.post("/{model_id}/deactivate", response_model=ModelDetail)
def deactivate_model(
    model_id: uuid.UUID,
    body: ActivationRequest,
    by: Principal = Depends(_activate),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> ModelDetail:
    """Return to rules-only scoring."""
    return get_model_detail(service.deactivate(db, model_id, body.reason, by, ctx))
