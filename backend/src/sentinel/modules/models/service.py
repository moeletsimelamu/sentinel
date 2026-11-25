"""Register trained models from their manifests.

Registration trusts nothing in the folder: the manifest must describe this code's feature
schema, and the artefact's SHA-256 must match. The artefact is **never unpickled** here —
loading for inference (Phase 7) re-verifies the hash first.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.core.config import Settings
from sentinel.core.errors import NotFoundError, ValidationFailedError
from sentinel.features import FEATURE_SCHEMA_HASH, MODEL_FEATURES
from sentinel.modules.audit import service as audit
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.models.models import ModelEvaluation, ModelFamily, ModelVersion

MAX_MANIFEST_BYTES = 5 * 1024 * 1024
SPLITS = ("validation", "test", "holdout")


class RegistrationError(ValueError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def read_manifest(settings: Settings, folder: Path) -> dict[str, Any]:
    root = settings.models_dir
    if not _inside(root, folder):
        raise RegistrationError("Model folder must be inside the configured models directory")
    path = folder / "manifest.json"
    if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
        raise RegistrationError("manifest.json missing or too large")
    manifest: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    family = manifest.get("family")
    if family not in {f.value for f in ModelFamily}:
        raise RegistrationError(f"Unknown model family {family!r}")
    if manifest.get("feature_schema_hash") != FEATURE_SCHEMA_HASH:
        raise RegistrationError("Feature schema hash does not match this application")
    if family != ModelFamily.RULES_BASELINE:
        if manifest.get("feature_names") != list(MODEL_FEATURES):
            raise RegistrationError("Feature names do not match this application")
        artefact = folder / str(manifest.get("artefact") or "")
        if not artefact.is_file() or not _inside(folder, artefact):
            raise RegistrationError("Artefact missing")
        if _sha256(artefact) != manifest.get("artefact_sha256"):
            raise RegistrationError("Artefact hash does not match the manifest")
    return manifest


def _evaluations(model: ModelVersion, manifest: dict[str, Any]) -> list[ModelEvaluation]:
    out = []
    metrics = manifest.get("metrics") or {}
    thresholds = manifest.get("thresholds") or {}
    for split in SPLITS:
        block = metrics.get(split)
        if not block:
            continue
        # validation/test blocks hold several views; holdout is a single metrics dict
        views = {"budget": block.get("at_budget", block), "f1": block.get("at_f1")}
        for kind, values in views.items():
            if not values:
                continue
            out.append(
                ModelEvaluation(
                    model_version_id=model.id,
                    dataset_version=model.training_dataset_version,
                    split=split,
                    threshold_kind=kind,
                    threshold=thresholds.get(kind),
                    metrics=values,
                    ci95=block.get("ci95") if kind == "budget" else None,
                    curves=block.get("curves") if kind == "budget" else None,
                )
            )
    return out


def register(
    db: Session, settings: Settings, folder: Path, *, actor: Actor, ctx: RequestContext
) -> tuple[ModelVersion, bool]:
    """Register one model folder. Returns (model, created); idempotent by version."""
    manifest = read_manifest(settings, folder)
    existing = db.scalar(select(ModelVersion).where(ModelVersion.version == manifest["version"]))
    if existing is not None:
        if existing.artefact_sha256 != manifest.get("artefact_sha256"):
            raise RegistrationError("A different artefact is already registered under this version")
        return existing, False
    artefact = manifest.get("artefact")
    root = settings.models_dir.resolve()
    rel = (folder / artefact).resolve().relative_to(root) if artefact else None
    stored_manifest = {k: v for k, v in manifest.items() if k != "tuning_log"}
    model = ModelVersion(
        name=manifest["name"],
        version=manifest["version"],
        family=manifest["family"],
        artefact_path=rel.as_posix() if rel else None,
        artefact_sha256=manifest.get("artefact_sha256"),
        feature_schema_hash=manifest["feature_schema_hash"],
        feature_set_version=manifest["feature_set_version"],
        training_dataset_version=manifest["training_dataset_version"],
        selected_by_experiment=bool(manifest.get("selected")),
        manifest=stored_manifest,
    )
    db.add(model)
    db.flush()
    for evaluation in _evaluations(model, manifest):
        db.add(evaluation)
    audit.record(
        db,
        actor=actor,
        ctx=ctx,
        action="model.registered",
        entity_type="model_version",
        entity_id=model.id,
        after={
            "version": model.version,
            "family": model.family,
            "artefact_sha256": model.artefact_sha256,
            "training_dataset_version": model.training_dataset_version,
        },
    )
    db.commit()
    db.refresh(model)
    return model, True


def discover(settings: Settings) -> list[Path]:
    """Every ``<family>/<version>/manifest.json`` under the models directory."""
    root = settings.models_dir
    if not root.is_dir():
        return []
    return sorted(p.parent for p in root.glob("*/*/manifest.json"))


def list_models(db: Session) -> Sequence[ModelVersion]:
    return db.scalars(
        select(ModelVersion).order_by(ModelVersion.registered_at.desc(), ModelVersion.version)
    ).all()


def get_model(db: Session, model_id: uuid.UUID) -> ModelVersion:
    model = db.get(ModelVersion, model_id)
    if model is None:
        raise NotFoundError("Model not found")
    return model


def require_folder(settings: Settings, name: str) -> Path:  # pragma: no cover - CLI helper
    folder = settings.models_dir / name
    if not folder.is_dir():
        raise ValidationFailedError("Model folder not found")
    return folder
