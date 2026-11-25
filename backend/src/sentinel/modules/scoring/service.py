"""Score transactions: features → rules → risk assessment (append-only).

Phase 4 runs in rules-only mode; Phase 7 adds the model component and alert policy.
Scoring loads, for every customer in the batch, all of their transactions from 90 days
before the earliest target up to the latest target, so features see the full prior
history even when it came from other imports.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, insert, select
from sqlalchemy.orm import Session

from sentinel.core.config import Settings
from sentinel.core.errors import NotFoundError
from sentinel.core.ids import uuid7
from sentinel.core.logging import get_logger
from sentinel.detection.engine import evaluate
from sentinel.detection.risk import Band, ScoringSpec, combine
from sentinel.features.behaviour import FEATURE_SET_VERSION, W_90D, TxnRecord, build_feature_rows
from sentinel.modules.audit import service as audit
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.auth.schemas import Principal
from sentinel.modules.ingestion.models import ImportBatch
from sentinel.modules.jobs import kinds
from sentinel.modules.jobs import service as jobs
from sentinel.modules.jobs.models import ProcessingJob
from sentinel.modules.rules import service as rules
from sentinel.modules.scoring.models import RiskAssessment, RuleHitRow, ScoringConfig
from sentinel.modules.transactions.models import Transaction

log = get_logger(__name__)
CHUNK = 1000


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


def score_transactions(
    db: Session,
    targets: Sequence[Transaction],
    *,
    job_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assess ``targets`` with the current rule set and active scoring config.

    Runs in the caller's transaction; returns a summary for the job result.
    """
    if not targets:
        return {"assessed": 0, "rule_hits": 0, "by_category": {}, "hits_by_rule": {}}
    rule_set = rules.current_rule_set(db)
    configured = rules.configured_rules(rule_set)
    cfg = active_scoring_config(db)
    spec = to_spec(cfg)
    rows = build_feature_rows(_load_history(db, targets), {t.id for t in targets})
    now = now or datetime.now(UTC)

    assessments: list[dict[str, Any]] = []
    hit_rows: list[dict[str, Any]] = []
    by_category: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    for t in targets:
        row = rows[t.id]
        hits = evaluate(configured, row)
        result = combine(hits, spec)
        assessment_id = uuid7()
        assessments.append(
            {
                "id": assessment_id,
                "transaction_id": t.id,
                "mode": result.mode.value,
                "rule_score": result.rule_score,
                "model_score": None,
                "model_version_id": None,
                "risk_score": result.risk_score,
                "risk_category": result.category,
                "force_alert": result.force_alert,
                "scoring_config_id": cfg.id,
                "rule_set_id": rule_set.id,
                "feature_set_version": FEATURE_SET_VERSION,
                "features": row.as_dict(),
                "reasons": list(result.reasons),
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

    for i in range(0, len(assessments), CHUNK):
        db.execute(insert(RiskAssessment), assessments[i : i + CHUNK])
    for i in range(0, len(hit_rows), CHUNK):
        db.execute(insert(RuleHitRow), hit_rows[i : i + CHUNK])
    return {
        "assessed": len(assessments),
        "rule_hits": len(hit_rows),
        "rule_set_version": rule_set.version,
        "scoring_config_version": cfg.version,
        "by_category": dict(sorted(by_category.items())),
        "hits_by_rule": dict(sorted(by_rule.items())),
    }


def batch_targets(db: Session, batch_id: uuid.UUID, *, rescore: bool) -> list[Transaction]:
    stmt = select(Transaction).where(Transaction.batch_id == batch_id)
    if not rescore:  # first scoring: skip anything already assessed (safe on retry)
        stmt = stmt.where(~exists().where(RiskAssessment.transaction_id == Transaction.id))
    return list(db.scalars(stmt.order_by(Transaction.occurred_at, Transaction.id)))


def process_score_batch(
    db: Session, payload: dict[str, Any], job_id: uuid.UUID | None = None
) -> dict[str, Any]:
    """Job handler for ``score_batch``. One transaction: all assessments or none."""
    batch_id = uuid.UUID(str(payload["batch_id"]))
    rescore = bool(payload.get("rescore", False))
    targets = batch_targets(db, batch_id, rescore=rescore)
    summary = score_transactions(db, targets, job_id=job_id)
    summary["batch_id"] = str(batch_id)
    summary["rescore"] = rescore
    log.info("batch_scored", batch_id=str(batch_id), assessed=summary["assessed"])
    return summary


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
