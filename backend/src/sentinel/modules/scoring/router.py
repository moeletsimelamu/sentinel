from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from sentinel.core.config import Settings, get_settings
from sentinel.core.db import get_db
from sentinel.core.permissions import Permission
from sentinel.modules.audit.context import RequestContext, get_request_context
from sentinel.modules.auth.dependencies import RequirePermission
from sentinel.modules.auth.schemas import Principal
from sentinel.modules.jobs.models import ProcessingJob
from sentinel.modules.scoring import service
from sentinel.modules.scoring.schemas import (
    AssessmentOut,
    DetectionRunOut,
    DetectionRunRequest,
    ScoringConfigChange,
    ScoringConfigOut,
)

detection_router = APIRouter(prefix="/detection", tags=["detection"])
assessments_router = APIRouter(prefix="/transactions", tags=["detection"])
scoring_config_router = APIRouter(prefix="/scoring-config", tags=["detection"])
scoring_configs_router = APIRouter(prefix="/scoring-configs", tags=["detection"])

_run = RequirePermission(Permission.DETECTION_RUN)
_read_txn = RequirePermission(Permission.TRANSACTIONS_READ)
_read_rules = RequirePermission(Permission.RULES_READ)
_configure = RequirePermission(Permission.SCORING_CONFIGURE)


def _run_out(job: ProcessingJob) -> DetectionRunOut:
    return DetectionRunOut(
        id=job.id,
        status=job.status,
        batch_id=uuid.UUID(str(job.payload["batch_id"])),
        rescore=bool(job.payload.get("rescore", False)),
        attempts=job.attempts,
        last_error=job.last_error,
        result=job.result,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


@detection_router.post(
    "/runs", response_model=DetectionRunOut, status_code=status.HTTP_202_ACCEPTED
)
def start_detection_run(
    body: DetectionRunRequest,
    by: Principal = Depends(_run),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> DetectionRunOut:
    """Re-score an import with the current rules. Earlier assessments are kept."""
    job = service.request_detection_run(db, settings, body.batch_id, by, ctx)
    return _run_out(job)


@detection_router.get(
    "/runs/{run_id}", response_model=DetectionRunOut, dependencies=[Depends(_read_txn)]
)
def get_detection_run(run_id: uuid.UUID, db: Session = Depends(get_db)) -> DetectionRunOut:
    return _run_out(service.get_run(db, run_id))


@assessments_router.get(
    "/{transaction_id}/assessments",
    response_model=list[AssessmentOut],
    dependencies=[Depends(_read_txn)],
)
def list_assessments(
    transaction_id: uuid.UUID, db: Session = Depends(get_db)
) -> list[AssessmentOut]:
    """Assessment history for a transaction, newest first."""
    return [AssessmentOut.model_validate(a) for a in service.assessments_for(db, transaction_id)]


@scoring_config_router.get("", response_model=ScoringConfigOut, dependencies=[Depends(_read_rules)])
def get_scoring_config(db: Session = Depends(get_db)) -> ScoringConfigOut:
    return ScoringConfigOut.model_validate(service.active_scoring_config(db))


@scoring_configs_router.get(
    "", response_model=list[ScoringConfigOut], dependencies=[Depends(_read_rules)]
)
def list_scoring_configs(db: Session = Depends(get_db)) -> list[ScoringConfigOut]:
    return [ScoringConfigOut.model_validate(c) for c in service.list_configs(db)]


@scoring_configs_router.post(
    "", response_model=ScoringConfigOut, status_code=status.HTTP_201_CREATED
)
def create_scoring_config(
    body: ScoringConfigChange,
    by: Principal = Depends(_configure),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_request_context),
) -> ScoringConfigOut:
    """New active configuration (weights, rule cap, bands, alert threshold). Audited."""
    return ScoringConfigOut.model_validate(service.create_config(db, body, by, ctx))
