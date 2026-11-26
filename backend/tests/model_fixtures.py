"""Tiny real models in the artefact/manifest format produced by ``sentinel-ml``.

Trained on random data shaped like ``MODEL_FEATURES`` so the backend can test loading,
scoring and explanations without depending on the ``ml`` package.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import RandomForestClassifier
from sklearn.frozen import FrozenEstimator
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from sentinel.features import FEATURE_SCHEMA_HASH, MODEL_FEATURES

REFERENCE = {"population_median_amount_zar": 300.0, "category_p95_amount_zar": {"groceries": 900.0}}


def training_frame(n: int = 600, seed: int = 0) -> tuple[pd.DataFrame, np.ndarray]:
    rng = np.random.default_rng(seed)
    features = pd.DataFrame(rng.normal(size=(n, len(MODEL_FEATURES))), columns=list(MODEL_FEATURES))
    # "fraud" when the amount ratio and a new device are both high: a learnable interaction
    y = (features["log_amount_vs_cust_median_90d"] + features["is_new_device_90d"] > 1.5).to_numpy()
    return features, y


def build_model(root: Path, family: str = "random_forest", version: str | None = None) -> Path:
    features, y = training_frame()
    if family == "random_forest":
        base: Any = RandomForestClassifier(n_estimators=15, max_depth=5, random_state=0).fit(
            features, y
        )
    else:
        base = Pipeline(
            [("scale", StandardScaler()), ("model", LogisticRegression(max_iter=2000))]
        ).fit(features, y)
    calibrated = CalibratedClassifierCV(FrozenEstimator(base), method="sigmoid").fit(features, y)
    version = version or f"{family}-test-s0"
    folder = root / family / version
    folder.mkdir(parents=True, exist_ok=True)
    artefact = folder / "model.joblib"
    joblib.dump({"family": family, "estimator": calibrated}, artefact, compress=3)
    digest = hashlib.sha256(artefact.read_bytes()).hexdigest()
    manifest = {
        "manifest_version": 1,
        "name": f"sentinel-{family}",
        "version": version,
        "family": family,
        "artefact": "model.joblib",
        "artefact_sha256": digest,
        "score_kind": "probability",
        "training_dataset_version": "unit-test",
        "feature_set_version": "behaviour-v2",
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "feature_names": list(MODEL_FEATURES),
        "feature_reference": REFERENCE,
        "calibration": "sigmoid",
        "thresholds": {"budget": 0.5, "f1": 0.5},
        "metrics": {},
        "selected": True,
        "libraries": {"scikit_learn": sklearn.__version__},
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder
