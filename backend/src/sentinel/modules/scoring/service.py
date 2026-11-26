"""Score transactions: features → rules → risk assessment (append-only).

Uses the active model when one is activated (combined mode), otherwise rules only.
Alerts are raised from the returned outcome by the alerts module.
Scoring loads, for every customer in the batch, all of their transactions from 90 days
before the earliest target up to the latest target, so features see the full prior
history even when it came from other imports.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, func, insert, select, text
from sqlalchemy.orm import Session

from sentinel.core.config import Settings
from sentinel.core.errors import ConflictError, NotFoundError, ValidationFailedError
from sentinel.core.ids import uuid7
from sentinel.core.logging import get_logger
from sentinel.detection.engine import evaluate
from sentinel.detection.risk import Band, ScoringSpec, combine
from sentinel.features.behaviour import FEATURE_SET_VERSION, W_90D, TxnRecord, build_feature_rows
from sentinel.inference import Contribution, LoadedModel, ModelLoadError, load_model
from sentinel.inference.labels import model_reason
from sentinel.modules.audit import service as audit
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.auth.schemas import Principal
from sentinel.modules.ingestion.models import ImportBatch
from sentinel.modules.jobs import kinds
from sentinel.modules.jobs import service as jobs
from sentinel.modules.jobs.models import ProcessingJob
from sentinel.modules.models.models import ModelStatus, ModelVersion
from sentinel.modules.rules import service as rules
from sentinel.modules.scoring.models import RiskAssessment, RuleHitRow, ScoringConfig
from sentinel.modules.scoring.schemas import ScoringConfigChange
from sentinel.modules.transactions.models import Transaction

log = get_logger(__name__)
CHUNK = 1000
MAX_EXPLAINED = 5000  # explanations cost ~3 ms each; bounded per batch


def active_scoring_config(db: Session) -> ScoringConfig:
    cfg = db.scalar(select(ScoringConfig).where(ScoringConfig.is_active))
    if cfg is None:  # pragma: no cover - seeded by migration 0003
        raise NotFoundError("No active scoring configuration")
    return cfg


def to_spec(cfg: ScoringConfig) -> ScoringSpec:
    bands = sorted(
        (Band(b["name"], int(b["min_score"])) for b in cfg.bands), key=lambda b: b.min_score
    )
    return ScoringSpec(
        w_rule=float(cfg.w_rule),
        w_model=float(cfg.w_model),
        rule_cap=cfg.rule_cap,
        bands=tuple(bands),
        alert_threshold=cfg.alert_threshold,
    )


def _record(t: Transaction) -> TxnRecord:
    return TxnRecord(
        id=t.id,
        customer_id=t.customer_id,
        occurred_at=t.occurred_at,
        amount=float(t.amount),
        currency=t.currency,
        status=t.status,
        merchant_category=t.merchant_category,
        channel=t.channel,
        city=t.city,
        lat=t.lat,
        lon=t.lon,
        device_id_hash=t.device_id_hash,
        txn_type=t.txn_type,
    )


def _load_history(db: Session, targets: Sequence[Transaction]) -> list[TxnRecord]:
    customers = sorted({t.customer_id for t in targets})
    start = min(t.occurred_at for t in targets) - W_90D
    end = max(t.occurred_at for t in targets)
    history: list[TxnRecord] = []
    for i in range(0, len(customers), CHUNK):
        rows = db.scalars(
            select(Transaction).where(
                Transaction.customer_id.in_(customers[i : i + CHUNK]),
                Transaction.occurred_at >= start,
                Transaction.occurred_at <= end,
            )
        )
        history.extend(_record(t) for t in rows)
    return history


@dataclass(frozen=True)
class Assessed:
    """What the alert policy needs to know about one new assessment."""

    transaction: Transaction
    assessment_id: uuid.UUID
    risk_score: int
    risk_category: str
    force_alert: bool
    alertable: bool


@dataclass
class ScoringOutcome:
    summary: dict[str, Any]
    assessed: list[Assessed] = field(default_factory=list)


_MODEL_CACHE: dict[uuid.UUID, LoadedModel] = {}


def active_model(db: Session) -> ModelVersion | None:
    return db.scalar(select(ModelVersion).where(ModelVersion.status == ModelStatus.ACTIVE))


def load_registered(settings: Settings, model: ModelVersion) -> LoadedModel:
    """Load (and cache) a registered model, re-verifying its hash against the registry."""
    cached = _MODEL_CACHE.get(model.id)
    if cached is not None:
        return cached
    if not model.artefact_path or not model.artefact_sha256:
        raise ModelLoadError("This model version has no artefact")
    folder = (settings.models_dir / model.artefact_path).parent
    loaded = load_model(folder, expected_sha256=model.artefact_sha256, version=model.version)
    _MODEL_CACHE.clear()  # keep only the model in use
    _MODEL_CACHE[model.id] = loaded
    return loaded


def explanation_floor(spec: ScoringSpec) -> int:
    """Explain everything in the band just below the alert threshold and above."""
    below = [b.min_score for b in spec.bands if b.min_score < spec.alert_threshold]
    return max(below) if below else 0


def score_transactions(
    db: Session,
    targets: Sequence[Transaction],
    *,
    settings: Settings,
    job_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> ScoringOutcome:
    """Assess ``targets`` with the current rule set, scoring config and active model.

    With no active model the score is rules-only. Model explanations are computed for
    transactions at or above the explanation floor or forced to alert. Runs in the
    caller's transaction.
    """
    if not targets:
        return ScoringOutcome(
            {"assessed": 0, "rule_hits": 0, "by_category": {}, "hits_by_rule": {}}
        )
    rule_set = rules.current_rule_set(db)
    configured = rules.configured_rules(rule_set)
    cfg = active_scoring_config(db)
    spec = to_spec(cfg)
    registered = active_model(db)
    model = load_registered(settings, registered) if registered is not None else None
    rows = build_feature_rows(_load_history(db, targets), {t.id for t in targets})
    ordered = [rows[t.id] for t in targets]
    model_scores = model.predict(ordered) if model is not None else None
    now = now or datetime.now(UTC)

    results = []
    for i, t in enumerate(targets):
        hits = evaluate(configured, ordered[i])
        p = float(model_scores[i]) if model_scores is not None else None
        results.append((t, ordered[i], hits, combine(hits, spec, p)))

    floor = explanation_floor(spec)
    explain_idx = [
        i for i, (_, _, _, r) in enumerate(results) if r.force_alert or r.risk_score >= floor
    ][:MAX_EXPLAINED]
    explanations: dict[int, list[Contribution]] = {}
    if model is not None and explain_idx:
        explained = model.explain([ordered[i] for i in explain_idx])
        explanations = dict(zip(explain_idx, explained, strict=True))

    assessments: list[dict[str, Any]] = []
    hit_rows: list[dict[str, Any]] = []
    outcome = ScoringOutcome({})
    by_category: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    for i, (t, row, hits, result) in enumerate(results):
        assessment_id = uuid7()
        reasons = list(result.reasons)
        contributions = None
        if model is not None and i in explanations:
            items = explanations[i]
            contributions = {
                "space": model.explanation_space,
                "items": [c.as_dict() for c in items],
            }
            reasons += [
                model_reason(c.feature, c.contribution) for c in items[:3] if c.contribution > 0
            ]
        assessments.append(
            {
                "id": assessment_id,
                "transaction_id": t.id,
                "mode": result.mode.value,
                "rule_score": result.rule_score,
                "model_score": result.model_score,
                "model_version_id": registered.id if registered is not None else None,
                "risk_score": result.risk_score,
                "risk_category": result.category,
                "force_alert": result.force_alert,
                "scoring_config_id": cfg.id,
                "rule_set_id": rule_set.id,
                "feature_set_version": FEATURE_SET_VERSION,
                "features": row.as_dict(),
                "reasons": reasons,
                "contributions": contributions,
                "job_id": job_id,
                "assessed_at": now,
            }
        )
        for h in hits:
            by_rule[h.code] += 1
            hit_rows.append(
                {
                    "assessment_id": assessment_id,
                    "rule_version_id": h.rule_version_id,
                    "rule_code": h.code,
                    "weight_applied": h.weight,
                    "severity": h.severity.value,
                    "force_alert": h.force_alert,
                    "observed": h.observed,
                    "threshold": h.threshold,
                    "explanation": h.explanation,
                }
            )
        by_category[result.category] += 1
        outcome.assessed.append(
            Assessed(
                transaction=t,
                assessment_id=assessment_id,
                risk_score=result.risk_score,
                risk_category=result.category,
                force_alert=result.force_alert,
                alertable=result.force_alert or result.risk_score >= spec.alert_threshold,
            )
        )

    for i in range(0, len(assessments), CHUNK):
        db.execute(insert(RiskAssessment), assessments[i : i + CHUNK])
    for i in range(0, len(hit_rows), CHUNK):
        db.execute(insert(RuleHitRow), hit_rows[i : i + CHUNK])
    outcome.summary = {
        "assessed": len(assessments),
        "rule_hits": len(hit_rows),
        "explained": len(explanations),
        "mode": "combined" if model is not None else "rules_only",
        "model_version": registered.version if registered is not None else None,
        "rule_set_version": rule_set.version,
        "scoring_config_version": cfg.version,
        "alertable": sum(a.alertable for a in outcome.assessed),
        "by_category": dict(sorted(by_category.items())),
        "hits_by_rule": dict(sorted(by_rule.items())),
    }
    return outcome


def batch_targets(db: Session, batch_id: uuid.UUID, *, rescore: bool) -> list[Transaction]:
    stmt = select(Transaction).where(Transaction.batch_id == batch_id)
    if not rescore:  # first scoring: skip anything already assessed (safe on retry)
        stmt = stmt.where(~exists().where(RiskAssessment.transaction_id == Transaction.id))
    return list(db.scalars(stmt.order_by(Transaction.occurred_at, Transaction.id)))


def enqueue_batch_scoring(
    db: Session, settings: Settings, batch_id: uuid.UUID, *, rescore: bool = False
) -> ProcessingJob:
    """Queue scoring for a batch in the caller's transaction."""
    return jobs.enqueue(
        db, kinds.SCORE_BATCH, {"batch_id": str(batch_id), "rescore": rescore}, settings=settings
    )


def request_detection_run(
    db: Session,
    settings: Settings,
    batch_id: uuid.UUID,
    by: Principal,
    ctx: RequestContext,
) -> ProcessingJob:
    """Re-score every transaction in a batch with the current configuration."""
    if db.get(ImportBatch, batch_id) is None:
        raise NotFoundError("Import not found")
    job = enqueue_batch_scoring(db, settings, batch_id, rescore=True)
    audit.record(
        db,
        actor=Actor(by.id, by.role.value),
        ctx=ctx,
        action="detection.run_requested",
        entity_type="import_batch",
        entity_id=batch_id,
        after={"job_id": job.id, "rescore": True},
    )
    db.commit()
    return job


def get_run(db: Session, run_id: uuid.UUID) -> ProcessingJob:
    job = jobs.get_job(db, run_id)
    if job is None or job.kind != kinds.SCORE_BATCH:
        raise NotFoundError("Detection run not found")
    return job


def assessments_for(db: Session, transaction_id: uuid.UUID) -> Sequence[RiskAssessment]:
    if db.get(Transaction, transaction_id) is None:
        raise NotFoundError("Transaction not found")
    return db.scalars(
        select(RiskAssessment)
        .where(RiskAssessment.transaction_id == transaction_id)
        .order_by(RiskAssessment.assessed_at.desc(), RiskAssessment.id.desc())
    ).all()


# ------------------------------------------------------------- scoring config

_SCORING_LOCK = 0x53_45_4E_53_43_4F_52  # serialises config versions ("SENSCOR")


def list_configs(db: Session) -> Sequence[ScoringConfig]:
    return db.scalars(select(ScoringConfig).order_by(ScoringConfig.version.desc())).all()


def _config_snapshot(cfg: ScoringConfig) -> dict[str, Any]:
    return {
        "version": cfg.version,
        "w_rule": float(cfg.w_rule),
        "w_model": float(cfg.w_model),
        "rule_cap": cfg.rule_cap,
        "bands": cfg.bands,
        "alert_threshold": cfg.alert_threshold,
    }


def create_config(
    db: Session, change: ScoringConfigChange, by: Principal, ctx: RequestContext
) -> ScoringConfig:
    """New active scoring configuration (the previous one is kept, inactive)."""
    db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _SCORING_LOCK})
    current = active_scoring_config(db)
    if change.expected_version != current.version:
        raise ConflictError(
            "The scoring configuration changed. Reload and try again.",
            code="stale_version",
            details={"current_version": current.version},
        )
    latest = db.scalar(select(func.max(ScoringConfig.version))) or current.version
    new = ScoringConfig(
        version=latest + 1,
        w_rule=round(change.w_rule, 3),
        w_model=round(1 - round(change.w_rule, 3), 3),
        rule_cap=change.rule_cap,
        bands=[b.model_dump() for b in change.bands],
        alert_threshold=change.alert_threshold,
        is_active=True,
        change_reason=change.change_reason,
        created_by_id=by.id,
    )
    before, after = audit.diff(_config_snapshot(current), _config_snapshot(new))
    if set(before) == {"version"}:
        raise ValidationFailedError(
            "The change does not modify the configuration", code="no_changes"
        )
    current.is_active = False
    db.flush()  # one active config at a time (partial unique index)
    db.add(new)
    audit.record(
        db,
        actor=Actor(by.id, by.role.value),
        ctx=ctx,
        action="scoring_config.version_created",
        entity_type="scoring_config",
        entity_id=new.id,
        before=before,
        after={**after, "change_reason": change.change_reason},
    )
    db.commit()
    db.refresh(new)
    return new
