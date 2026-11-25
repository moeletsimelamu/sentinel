"""Model artefacts and manifests: ``models/<family>/<version>/{model.joblib, manifest.json}``.

A manifest records everything needed to trust and reproduce a model: dataset version,
feature schema hash and names, the training-split reference statistics, drift reference
bins, calibration, thresholds, metrics, library versions and the artefact's SHA-256.

joblib files are pickles and can execute code when loaded. Loaders must call
``verify`` (hash + schema) before ``joblib.load``, and only artefacts produced by this
pipeline and registered in the database are ever loaded by the application.
"""

from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
from sentinel.features import FEATURE_SCHEMA_HASH, FEATURE_SET_VERSION, MODEL_FEATURES

from sentinel_ml import GENERATOR_VERSION
from sentinel_ml.train.candidates import Fitted

MANIFEST_VERSION = 1
DRIFT_BINS = 10


class ManifestError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def drift_reference(X: pd.DataFrame, scores: np.ndarray) -> dict[str, Any]:
    """Decile bin edges and training proportions per feature and for the score (for PSI)."""

    def bins(values: np.ndarray) -> dict[str, Any]:
        edges = np.unique(np.quantile(values, np.linspace(0, 1, DRIFT_BINS + 1)))
        inner = edges[1:-1]
        counts = np.bincount(np.searchsorted(inner, values, side="right"), minlength=len(edges) - 1)
        return {
            "edges": [float(e) for e in inner],
            "proportions": [float(c) / len(values) for c in counts],
        }

    return {
        "features": {c: bins(X[c].to_numpy(dtype=float)) for c in X.columns},
        "score": bins(np.asarray(scores, dtype=float)),
    }


def model_version(family: str, dataset_version: str, seed: int, digest: str) -> str:
    return f"{family}-{dataset_version}-s{seed}-{digest[:8]}"


@dataclass(frozen=True)
class SavedModel:
    folder: Path
    manifest: dict[str, Any]


def save_model(
    fitted: Fitted,
    root: Path,
    *,
    dataset_version: str,
    reference: dict[str, Any],
    drift: dict[str, Any],
    thresholds: dict[str, float],
    metrics: dict[str, Any],
    selected: bool,
    extra: dict[str, Any] | None = None,
) -> SavedModel:
    staging = root / fitted.family / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    artefact = staging / "model.joblib"
    payload = {"family": fitted.family, "estimator": fitted.estimator}
    if fitted.train_scores is not None:
        payload["train_scores"] = fitted.train_scores
    joblib.dump(payload, artefact, compress=3)
    digest = sha256_file(artefact)
    version = model_version(fitted.family, dataset_version, fitted.seed, digest)
    folder = root / fitted.family / version
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / "model.joblib"
    final.unlink(missing_ok=True)
    artefact.replace(final)
    staging.rmdir()
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "name": f"sentinel-{fitted.family}",
        "version": version,
        "family": fitted.family,
        "artefact": "model.joblib",
        "artefact_sha256": digest,
        "score_kind": "anomaly_rank" if fitted.family == "isolation_forest" else "probability",
        "training_dataset_version": dataset_version,
        "generator_version": GENERATOR_VERSION,
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "feature_names": list(MODEL_FEATURES),
        "feature_reference": reference,
        "drift_reference": drift,
        "params": fitted.params,
        "seed": fitted.seed,
        "calibration": fitted.calibration,
        "thresholds": thresholds,
        "metrics": metrics,
        "selected": selected,
        "libraries": {
            "python": platform.python_version(),
            "scikit_learn": sklearn.__version__,
            "numpy": np.__version__,
            "joblib": joblib.__version__,
        },
        **(extra or {}),
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return SavedModel(folder, manifest)


def save_rules_manifest(
    root: Path, *, dataset_version: str, params: dict[str, Any], metrics: dict[str, Any],
    thresholds: dict[str, float], changed: dict[str, Any], tuning: list[dict[str, Any]],
) -> SavedModel:  # fmt: skip
    """The rule baseline has no artefact: its 'model' is the rule parameters."""
    digest = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    version = f"rules_baseline-{dataset_version}-{digest[:8]}"
    folder = root / "rules_baseline" / version
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "name": "sentinel-rules-baseline",
        "version": version,
        "family": "rules_baseline",
        "artefact": None,
        "artefact_sha256": None,
        "score_kind": "rule_score",
        "training_dataset_version": dataset_version,
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "rule_params": params,
        "recommended_changes": changed,
        "tuning_log": tuning,
        "thresholds": thresholds,
        "metrics": metrics,
        "selected": False,
    }
    (folder / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return SavedModel(folder, manifest)


def verify(folder: Path) -> dict[str, Any]:
    """Check a saved model before loading it. Raises ``ManifestError`` on any mismatch."""
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ManifestError("Unsupported manifest version")
    if manifest["feature_schema_hash"] != FEATURE_SCHEMA_HASH:
        raise ManifestError("Feature schema does not match this code version")
    if manifest.get("artefact"):
        path = folder / manifest["artefact"]
        if not path.is_file():
            raise ManifestError("Artefact missing")
        if sha256_file(path) != manifest["artefact_sha256"]:
            raise ManifestError("Artefact hash mismatch; refusing to load")
    return manifest


def load_verified(folder: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = verify(folder)
    if not manifest.get("artefact"):
        raise ManifestError("This manifest has no model artefact")
    return manifest, joblib.load(folder / manifest["artefact"])
