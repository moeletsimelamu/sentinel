"""Job handler for ``score_batch``: assess a batch, then apply the alert policy.

Both steps run in one database transaction, so a batch is either fully assessed *and*
alerted, or not at all (safe to retry).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from sentinel.core.config import Settings
from sentinel.core.logging import get_logger
from sentinel.modules.alerts import service as alerts
from sentinel.modules.scoring import service as scoring

log = get_logger(__name__)


def process_score_batch(
    db: Session, settings: Settings, payload: dict[str, Any], job_id: uuid.UUID | None = None
) -> dict[str, Any]:
    batch_id = uuid.UUID(str(payload["batch_id"]))
    rescore = bool(payload.get("rescore", False))
    targets = scoring.batch_targets(db, batch_id, rescore=rescore)
    outcome = scoring.score_transactions(db, targets, settings=settings, job_id=job_id)
    summary = {**outcome.summary, **alerts.raise_alerts(db, outcome)}
    summary["batch_id"] = str(batch_id)
    summary["rescore"] = rescore
    log.info(
        "batch_scored",
        batch_id=str(batch_id),
        assessed=summary["assessed"],
        alerts_created=summary["alerts_created"],
    )
    return summary
