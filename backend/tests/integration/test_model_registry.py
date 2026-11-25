"""Model registry: manifest verification, idempotent registration, read API, immutability."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from sentinel.core.config import Settings, get_settings
from sentinel.core.permissions import Role
from sentinel.features import FEATURE_SCHEMA_HASH, MODEL_FEATURES
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.audit.models import AuditLog
from sentinel.modules.models import service
from sentinel.modules.models.models import ModelVersion
from tests.conftest import AuthHeaders

pytestmark = pytest.mark.integration

METRICS = {
    "validation": {
        "at_budget": {"pr_auc": 0.30, "recall": 0.40, "precision": 0.30, "alerts_per_1000": 10.0},
        "at_f1": {"pr_auc": 0.30, "recall": 0.35},
    },
    "test": {
        "at_budget": {"pr_auc": 0.55, "recall": 0.62, "precision": 0.4, "alerts_per_1000": 9.5},
        "at_f1": {"pr_auc": 0.55, "recall": 0.6},
        "ci95": {"pr_auc": [0.5, 0.6], "recall": [0.55, 0.7]},
        "curves": {"pr": [[0.0, 1.0], [1.0, 0.01]], "roc": [[0.0, 0.0], [1.0, 1.0]]},
    },
    "holdout": {"pr_auc": 0.5, "recall": 0.6, "precision": 0.35, "alerts_per_1000": 11.0},
}


def make_model(root: Path, version: str = "random_forest-x-s0-abcd1234", **overrides: Any) -> Path:
    folder = root / "random_forest" / version
    folder.mkdir(parents=True)
    artefact = folder / "model.joblib"
    artefact.write_bytes(b"not really a pickle - registration never loads it")
    manifest = {
        "manifest_version": 1,
        "name": "sentinel-random_forest",
        "version": version,
        "family": "random_forest",
        "artefact": "model.joblib",
        "artefact_sha256": hashlib.sha256(artefact.read_bytes()).hexdigest(),
        "training_dataset_version": "full-1.0.0-42",
        "feature_set_version": "behaviour-v2",
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "feature_names": list(MODEL_FEATURES),
        "thresholds": {"budget": 0.42, "f1": 0.37},
        "metrics": METRICS,
        "calibration": "isotonic",
        "params": {"max_depth": 16},
        "selected": True,
        "recommended_scoring": {"w_rule": 0.25, "w_model": 0.75},
        **overrides,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return get_settings().model_copy(update={"models_dir": tmp_path})


def _register(db: Session, settings: Settings, folder: Path) -> tuple[ModelVersion, bool]:
    return service.register(
        db, settings, folder, actor=Actor.system(), ctx=RequestContext.offline()
    )


def test_register_creates_version_evaluations_and_audit(db: Session, settings: Settings) -> None:
    model, created = _register(db, settings, make_model(settings.models_dir))
    assert created and model.status == "registered" and model.selected_by_experiment
    assert model.artefact_path == "random_forest/random_forest-x-s0-abcd1234/model.joblib"
    kinds = {(e.split, e.threshold_kind) for e in model.evaluations}
    assert kinds == {
        ("validation", "budget"),
        ("validation", "f1"),
        ("test", "budget"),
        ("test", "f1"),
        ("holdout", "budget"),
    }
    test = next(e for e in model.evaluations if e.split == "test" and e.threshold_kind == "budget")
    assert test.threshold == 0.42 and test.ci95 and test.curves
    entry = db.scalars(select(AuditLog).where(AuditLog.action == "model.registered")).one()
    assert entry.entity_id == str(model.id) and entry.actor_role == "system"


def test_registration_is_idempotent(db: Session, settings: Settings) -> None:
    folder = make_model(settings.models_dir)
    first, _ = _register(db, settings, folder)
    again, created = _register(db, settings, folder)
    assert not created and again.id == first.id


def test_tampered_artefact_is_refused(db: Session, settings: Settings) -> None:
    folder = make_model(settings.models_dir)
    (folder / "model.joblib").write_bytes(b"swapped")
    with pytest.raises(service.RegistrationError, match="hash"):
        _register(db, settings, folder)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"feature_schema_hash": "0" * 16}, "schema"),
        ({"feature_names": ["something_else"]}, "Feature names"),
        ({"family": "neural_net"}, "family"),
        ({"artefact": "../../outside.joblib"}, "Artefact missing"),
    ],
)
def test_invalid_manifests_are_refused(
    db: Session, settings: Settings, overrides: dict[str, Any], message: str
) -> None:
    folder = make_model(settings.models_dir, **overrides)
    with pytest.raises(service.RegistrationError, match=message):
        _register(db, settings, folder)


def test_folder_outside_models_dir_is_refused(
    db: Session, settings: Settings, tmp_path_factory: pytest.TempPathFactory
) -> None:
    elsewhere = make_model(tmp_path_factory.mktemp("elsewhere"))
    with pytest.raises(service.RegistrationError, match="inside"):
        _register(db, settings, elsewhere)


def test_rules_baseline_registers_without_artefact(db: Session, settings: Settings) -> None:
    folder = settings.models_dir / "rules_baseline" / "rules_baseline-x-1234"
    folder.mkdir(parents=True)
    manifest = {
        "manifest_version": 1,
        "name": "sentinel-rules-baseline",
        "version": "rules_baseline-x-1234",
        "family": "rules_baseline",
        "artefact": None,
        "artefact_sha256": None,
        "training_dataset_version": "full-1.0.0-42",
        "feature_set_version": "behaviour-v2",
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "rule_params": {"BEHAVIOUR_SHIFT": {"min_shift": 0.9, "min_history": 20}},
        "recommended_changes": {"BEHAVIOUR_SHIFT": {"min_shift": 0.9}},
        "tuning_log": [{"big": "x" * 100}],
        "metrics": {"validation": METRICS["validation"]},
        "thresholds": {"budget": 35.0},
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    model, _ = _register(db, settings, folder)
    assert model.artefact_path is None and "tuning_log" not in model.manifest


def test_discover_finds_manifests(settings: Settings) -> None:
    make_model(settings.models_dir, version="a")
    make_model(settings.models_dir, version="b")
    assert [p.name for p in service.discover(settings)] == ["a", "b"]


def test_models_api(
    client: TestClient, auth_headers: AuthHeaders, db: Session, settings: Settings
) -> None:
    model, _ = _register(db, settings, make_model(settings.models_dir))
    auditor = auth_headers(Role.AUDITOR)
    listed = client.get("/api/v1/models", headers=auditor).json()
    row = next(m for m in listed if m["id"] == str(model.id))
    assert row["test_pr_auc"] == 0.55 and row["test_recall_at_budget"] == 0.62
    detail = client.get(f"/api/v1/models/{model.id}", headers=auditor).json()
    assert detail["calibration"] == "isotonic"
    assert detail["recommended_scoring"] == {"w_rule": 0.25, "w_model": 0.75}
    assert len(detail["evaluations"]) == 5
    assert client.get(f"/api/v1/models/{model.id}x", headers=auditor).status_code == 422


def test_evaluations_are_append_only(db: Session, settings: Settings) -> None:
    _register(db, settings, make_model(settings.models_dir))
    with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
        db.execute(text("UPDATE model_evaluations SET threshold = 0"))


def test_only_one_model_can_be_active(db: Session, settings: Settings) -> None:
    a, _ = _register(db, settings, make_model(settings.models_dir, version="a"))
    b, _ = _register(db, settings, make_model(settings.models_dir, version="b"))
    a.status = "active"
    db.flush()
    b.status = "active"
    with pytest.raises(IntegrityError), db.begin_nested():
        db.flush()
