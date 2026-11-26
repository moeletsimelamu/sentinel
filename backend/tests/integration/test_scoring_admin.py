"""Scoring configuration versions, model activation and model-based scoring."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.core.config import Settings, get_settings
from sentinel.core.permissions import Role
from sentinel.main import app
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.audit.models import AuditLog
from sentinel.modules.models import service as models
from sentinel.modules.scoring.models import RiskAssessment
from sentinel.modules.transactions.models import Transaction
from sentinel.worker import default_handlers, process_next
from tests.conftest import AuthHeaders
from tests.integration.test_alerts import T0, travel_pair
from tests.model_fixtures import build_model

pytestmark = pytest.mark.integration

BANDS = [
    {"name": "low", "min_score": 0},
    {"name": "medium", "min_score": 40},
    {"name": "high", "min_score": 70},
    {"name": "critical", "min_score": 90},
]


def change(**kw: Any) -> dict[str, Any]:
    return {
        "expected_version": 1,
        "w_rule": 0.25,
        "rule_cap": 100,
        "bands": BANDS,
        "alert_threshold": 60,
        "change_reason": "Hybrid weights from the Phase 6 experiment",
        **kw,
    }


# -------------------------------------------------------------- scoring config


def test_scoring_config_versions(
    client: TestClient, auth_headers: AuthHeaders, db: Session
) -> None:
    admin = auth_headers(Role.ADMIN)
    resp = client.post("/api/v1/scoring-configs", json=change(), headers=admin)
    assert resp.status_code == 201, resp.text
    new = resp.json()
    assert new["version"] == 2 and new["w_rule"] == 0.25 and new["w_model"] == 0.75
    active = client.get("/api/v1/scoring-config", headers=admin).json()
    assert active["version"] == 2 and active["alert_threshold"] == 60
    history = client.get("/api/v1/scoring-configs", headers=admin).json()
    assert [c["version"] for c in history] == [2, 1] and history[1]["is_active"] is False

    stale = client.post("/api/v1/scoring-configs", json=change(w_rule=0.4), headers=admin)
    assert stale.status_code == 409 and stale.json()["error"]["details"] == {"current_version": 2}
    same = client.post("/api/v1/scoring-configs", json=change(expected_version=2), headers=admin)
    assert same.status_code == 422 and same.json()["error"]["code"] == "no_changes"
    entry = db.scalars(
        select(AuditLog).where(AuditLog.action == "scoring_config.version_created")
    ).one()
    assert entry.before is not None and entry.before["w_rule"] == 0.5
    assert entry.after is not None and entry.after["w_rule"] == 0.25


@pytest.mark.parametrize(
    "bad",
    [
        {"bands": [{"name": "low", "min_score": 10}, {"name": "high", "min_score": 70}]},
        {"bands": [{"name": "low", "min_score": 0}, {"name": "low", "min_score": 70}]},
        {"bands": [{"name": "low", "min_score": 0}, {"name": "high", "min_score": 0}]},
        {"w_rule": 1.5},
        {"alert_threshold": 0},
        {"change_reason": "short"},
    ],
)
def test_scoring_config_validation(
    client: TestClient, auth_headers: AuthHeaders, bad: dict[str, Any]
) -> None:
    resp = client.post(
        "/api/v1/scoring-configs", json=change(**bad), headers=auth_headers(Role.ADMIN)
    )
    assert resp.status_code == 422


def test_analysts_cannot_change_scoring(client: TestClient, auth_headers: AuthHeaders) -> None:
    resp = client.post("/api/v1/scoring-configs", json=change(), headers=auth_headers(Role.ANALYST))
    assert resp.status_code == 403


# ---------------------------------------------------------------- activation


@pytest.fixture
def settings(tmp_path: Path) -> Iterator[Settings]:
    custom = get_settings().model_copy(update={"models_dir": tmp_path})
    app.dependency_overrides[get_settings] = lambda: custom
    yield custom
    app.dependency_overrides.pop(get_settings, None)


def register(db: Session, settings: Settings, family: str = "random_forest") -> Any:
    folder = build_model(settings.models_dir, family)
    model, _ = models.register(
        db, settings, folder, actor=Actor.system(), ctx=RequestContext.offline()
    )
    return model


def activate(client: TestClient, headers: dict[str, str], model_id: Any) -> Any:
    return client.post(
        f"/api/v1/models/{model_id}/activate",
        json={"reason": "Selected on validation in Phase 6"},
        headers=headers,
    )


def test_activate_switch_and_deactivate(
    client: TestClient, auth_headers: AuthHeaders, db: Session, settings: Settings
) -> None:
    admin = auth_headers(Role.ADMIN)
    rf, lr = register(db, settings), register(db, settings, "logreg")
    resp = activate(client, admin, rf.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"
    assert activate(client, admin, rf.id).status_code == 409  # already active
    assert activate(client, admin, lr.id).json()["status"] == "active"
    statuses = {
        m["family"]: m["status"] for m in client.get("/api/v1/models", headers=admin).json()
    }
    assert statuses == {"random_forest": "registered", "logreg": "active"}
    entry = db.scalars(
        select(AuditLog).where(AuditLog.action == "model.activated").order_by(AuditLog.id.desc())
    ).first()
    assert entry is not None and entry.before == {"active_version": rf.version}
    off = client.post(
        f"/api/v1/models/{lr.id}/deactivate",
        json={"reason": "Back to rules-only mode"},
        headers=admin,
    )
    assert off.status_code == 200 and off.json()["status"] == "registered"
    again = client.post(
        f"/api/v1/models/{lr.id}/deactivate",
        json={"reason": "Back to rules-only mode"},
        headers=admin,
    )
    assert again.status_code == 409


def test_tampered_artefact_cannot_be_activated(
    client: TestClient, auth_headers: AuthHeaders, db: Session, settings: Settings
) -> None:
    rf = register(db, settings)
    artefact = settings.models_dir / rf.artefact_path
    artefact.write_bytes(artefact.read_bytes() + b"tampered")
    resp = activate(client, auth_headers(Role.ADMIN), rf.id)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "model_unloadable"


def test_only_admins_activate(
    client: TestClient, auth_headers: AuthHeaders, db: Session, settings: Settings
) -> None:
    rf = register(db, settings)
    assert activate(client, auth_headers(Role.ANALYST), rf.id).status_code == 403


# ------------------------------------------------------- model-based scoring


def run_worker_with(db: Session, settings: Settings) -> None:
    handlers = default_handlers(settings)
    while process_next(db, handlers, settings=settings, worker="test-worker"):
        pass


def test_active_model_scores_and_explains(
    client: TestClient, auth_headers: AuthHeaders, db: Session, settings: Settings
) -> None:
    admin, analyst = auth_headers(Role.ADMIN), auth_headers(Role.ANALYST)
    rf = register(db, settings)
    assert activate(client, admin, rf.id).status_code == 200
    resp = client.post(
        "/api/v1/transactions",
        json={"source_system": "model_test", "records": travel_pair("M1", "C-MOD-1", T0)},
        headers=analyst,
    )
    assert resp.status_code == 200
    run_worker_with(db, settings)

    rows = db.execute(
        select(Transaction.external_id, RiskAssessment)
        .join(RiskAssessment, RiskAssessment.transaction_id == Transaction.id)
        .where(Transaction.source_system == "model_test")
    ).all()
    by_ext = {ext: a for ext, a in rows}
    for a in by_ext.values():
        assert a.mode == "combined" and a.model_version_id == rf.id
        assert a.model_score is not None and 0 <= a.model_score <= 1
    forced = by_ext["M1-L"]
    assert forced.force_alert and forced.contributions is not None
    assert forced.contributions["space"].startswith("forest vote")
    assert 1 <= len(forced.contributions["items"]) <= 5
    assert by_ext["M1-H"].contributions is None  # low score, not explained

    off = client.post(
        f"/api/v1/models/{rf.id}/deactivate",
        json={"reason": "Back to rules-only mode"},
        headers=admin,
    )
    assert off.status_code == 200
    client.post(
        "/api/v1/transactions",
        json={"source_system": "model_test", "records": travel_pair("M2", "C-MOD-2", T0)},
        headers=analyst,
    )
    run_worker_with(db, settings)
    modes = db.scalars(
        select(RiskAssessment.mode)
        .join(Transaction, RiskAssessment.transaction_id == Transaction.id)
        .where(Transaction.customer_id.in_(
            select(Transaction.customer_id).where(Transaction.external_id == "M2-L")
        ))
    ).all()  # fmt: skip
    assert set(modes) == {"rules_only"}
