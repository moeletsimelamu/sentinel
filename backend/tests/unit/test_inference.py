"""Verified loading, scoring and explanation content."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from sentinel.features import MODEL_FEATURES
from sentinel.inference import ModelLoadError, load_model
from sentinel.inference.explain import forest_bias, tree_contributions
from sentinel.inference.labels import LABELS, model_reason
from sentinel.inference.loader import _uncalibrated
from tests.model_fixtures import build_model
from tests.unit.test_rules import QUIET


def _sha(folder: Path) -> str:
    return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))["artefact_sha256"]


@pytest.fixture(scope="module", params=["random_forest", "logreg"])
def model_folder(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_model(tmp_path_factory.mktemp("models"), request.param)


def _load(folder: Path):  # type: ignore[no-untyped-def]
    return load_model(folder, expected_sha256=_sha(folder), version=folder.name)


ROWS = [
    QUIET,
    replace(QUIET, amount_vs_cust_median_90d=40.0, is_new_device_90d=True, amount_zar=4000.0),
]


def test_loads_scores_and_explains(model_folder: Path) -> None:
    model = _load(model_folder)
    p = model.predict(ROWS)
    assert p.shape == (2,) and np.all((p >= 0) & (p <= 1))
    assert p[1] > p[0]  # the learnable "risky" pattern scores higher
    explanations = model.explain(ROWS, top_k=3)
    assert len(explanations) == 2 and len(explanations[1]) <= 3
    top = explanations[1]
    assert {c.feature for c in top} <= set(MODEL_FEATURES)
    magnitudes = [abs(c.contribution) for c in top]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_random_forest_contributions_are_additive(tmp_path: Path) -> None:
    model = _load(build_model(tmp_path, "random_forest"))
    frame = model.matrix(ROWS)
    forest = _uncalibrated(model.estimator)
    contrib = tree_contributions(forest, frame)
    raw = forest.predict_proba(frame)[:, 1]
    assert np.allclose(forest_bias(forest) + contrib.sum(axis=1), raw)


def test_logistic_contributions_are_exact(tmp_path: Path) -> None:
    model = _load(build_model(tmp_path, "logreg"))
    frame = model.matrix(ROWS)
    pipe = _uncalibrated(model.estimator)
    from sentinel.inference.explain import linear_contributions

    contrib = linear_contributions(pipe, frame)
    logit = pipe.decision_function(frame)
    assert np.allclose(contrib.sum(axis=1) + pipe.named_steps["model"].intercept_[0], logit)


def test_wrong_registered_hash_is_refused_before_unpickling(model_folder: Path) -> None:
    with pytest.raises(ModelLoadError, match="registered hash"):
        load_model(model_folder, expected_sha256="0" * 64, version=model_folder.name)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("feature_schema_hash", "0" * 16, "schema"),
        ("family", "rules_baseline", "cannot be used"),
        ("libraries", {"scikit_learn": "0.24.2"}, "scikit-learn"),
        ("version", "something-else", "version"),
    ],
)
def test_manifest_problems_are_refused(
    tmp_path: Path, key: str, value: object, message: str
) -> None:
    folder = build_model(tmp_path)
    mpath = folder / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    sha = manifest["artefact_sha256"]
    manifest[key] = value
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ModelLoadError, match=message):
        load_model(folder, expected_sha256=sha, version=folder.name)


def test_every_model_input_has_a_plain_label() -> None:
    assert set(MODEL_FEATURES) <= set(LABELS)


def test_model_reasons_never_accuse() -> None:
    for feature in MODEL_FEATURES:
        text = model_reason(feature, 0.12)
        assert text.startswith("Model: ") and "fraud" not in text.lower()
    assert "lowered" in model_reason("is_new_device_90d", -0.2)
