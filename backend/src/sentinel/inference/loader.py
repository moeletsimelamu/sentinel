"""Load a registered model safely and score feature rows.

joblib artefacts are pickles: loading one can execute code. ``load_model`` therefore
verifies, in order, that the file's SHA-256 equals the hash **recorded in the database
at registration** (not merely the manifest next to it), that the manifest describes this
code's feature schema, and that scikit-learn matches the training minor version. Only
then is the file unpickled.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn

from sentinel.features import (
    FEATURE_SCHEMA_HASH,
    MODEL_FEATURES,
    FeatureReference,
    FeatureRow,
    to_model_vector,
)
from sentinel.inference.explain import linear_contributions, tree_contributions

SUPPORTED_FAMILIES = frozenset({"logreg", "random_forest", "isolation_forest"})


class ModelLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class Contribution:
    feature: str
    value: float  # the model input value
    contribution: float  # signed effect on the model's raw score

    def as_dict(self) -> dict[str, float | str]:
        return {
            "feature": self.feature,
            "value": round(self.value, 4),
            "contribution": round(self.contribution, 4),
        }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _minor(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split(".")[:2] if p.isdigit())


@dataclass
class LoadedModel:
    version: str
    family: str
    reference: FeatureReference
    estimator: Any
    train_scores: np.ndarray | None
    explanation_space: str

    def matrix(self, rows: Sequence[FeatureRow]) -> pd.DataFrame:
        return pd.DataFrame(
            [to_model_vector(r, self.reference) for r in rows], columns=list(MODEL_FEATURES)
        )

    def predict(self, rows: Sequence[FeatureRow]) -> np.ndarray:
        """Calibrated probability (supervised) or 0-1 anomaly rank (isolation forest)."""
        if not rows:
            return np.empty(0)
        frame = self.matrix(rows)
        if self.family == "isolation_forest":
            assert self.train_scores is not None  # noqa: S101 - set for this family
            raw = -self.estimator.score_samples(frame)
            ranks = np.searchsorted(self.train_scores, raw, side="right")
            return np.asarray(ranks / len(self.train_scores), dtype=float)
        return np.asarray(self.estimator.predict_proba(frame)[:, 1], dtype=float)

    def explain(self, rows: Sequence[FeatureRow], top_k: int = 5) -> list[list[Contribution]]:
        """Largest signed per-feature contributions for each row (empty if unsupported)."""
        if not rows or self.family == "isolation_forest":
            return [[] for _ in rows]
        frame = self.matrix(rows)
        base = _uncalibrated(self.estimator)
        if self.family == "logreg":
            contrib = linear_contributions(base, frame)
        else:
            contrib = tree_contributions(base, frame)
        out = []
        values = frame.to_numpy(dtype=float)
        names = list(MODEL_FEATURES)
        for i in range(len(rows)):
            order = np.argsort(-np.abs(contrib[i]))[:top_k]
            out.append(
                [
                    Contribution(names[j], float(values[i, j]), float(contrib[i, j]))
                    for j in order
                    if contrib[i, j] != 0
                ]
            )
        return out


def _uncalibrated(estimator: Any) -> Any:
    """The fitted model inside ``CalibratedClassifierCV(FrozenEstimator(model))``."""
    inner = getattr(estimator, "calibrated_classifiers_", None)
    model = inner[0].estimator if inner else estimator
    return getattr(model, "estimator", model)  # unwrap FrozenEstimator


def load_model(folder: Path, *, expected_sha256: str, version: str) -> LoadedModel:
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        raise ModelLoadError("manifest.json missing")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    family = manifest.get("family")
    if family not in SUPPORTED_FAMILIES:
        raise ModelLoadError(f"Family {family!r} cannot be used for inference")
    if manifest.get("version") != version:
        raise ModelLoadError("Manifest version does not match the registered version")
    if manifest.get("feature_schema_hash") != FEATURE_SCHEMA_HASH:
        raise ModelLoadError("Feature schema does not match this application")
    artefact = folder / str(manifest.get("artefact") or "")
    if not artefact.is_file():
        raise ModelLoadError("Artefact missing")
    if _sha256(artefact) != expected_sha256:
        raise ModelLoadError("Artefact hash differs from the registered hash; refusing to load")
    trained_with = str((manifest.get("libraries") or {}).get("scikit_learn", ""))
    if _minor(trained_with) != _minor(sklearn.__version__):
        raise ModelLoadError(
            f"Model trained with scikit-learn {trained_with}; this application has "
            f"{sklearn.__version__}"
        )
    payload = joblib.load(artefact)  # safe: hash matches the registered artefact
    if not isinstance(payload, dict) or payload.get("family") != family:
        raise ModelLoadError("Unexpected artefact contents")
    return LoadedModel(
        version=version,
        family=family,
        reference=FeatureReference.from_dict(manifest["feature_reference"]),
        estimator=payload["estimator"],
        train_scores=payload.get("train_scores"),
        explanation_space="log-odds" if family == "logreg" else "forest vote (before calibration)",
    )
