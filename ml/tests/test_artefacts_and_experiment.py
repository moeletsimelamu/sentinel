"""Manifest/hash verification and a reproducible end-to-end experiment run."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from sentinel.features import FEATURE_SCHEMA_HASH, MODEL_FEATURES

from sentinel_ml.datasets.build import BuiltDataset
from sentinel_ml.train import candidates
from sentinel_ml.train.artefacts import (
    ManifestError,
    drift_reference,
    load_verified,
    save_model,
    save_rules_manifest,
    verify,
)
from sentinel_ml.train.data import load_dataset
from sentinel_ml.train.experiment import run


@pytest.fixture(scope="module")
def data(built: BuiltDataset):  # type: ignore[no-untyped-def]
    return load_dataset(built.path)


@pytest.fixture
def saved(data, tmp_path: Path):  # type: ignore[no-untyped-def]
    tr, va = data.splits["train"], data.splits["validation"]
    fitted = candidates.fit_supervised("logreg", {"C": 0.1}, tr, va, seed=0)
    return save_model(
        fitted,
        tmp_path,
        dataset_version=data.version,
        reference=data.reference.to_dict(),
        drift=drift_reference(tr.X, fitted.score(tr.X)),
        thresholds={"budget": 0.5, "f1": 0.4},
        metrics={},
        selected=True,
    )


def test_manifest_contents(saved) -> None:  # type: ignore[no-untyped-def]
    m = saved.manifest
    assert m["feature_schema_hash"] == FEATURE_SCHEMA_HASH
    assert m["feature_names"] == list(MODEL_FEATURES)
    assert m["version"].startswith("logreg-tiny-1.0.0-7-s0-")
    assert m["artefact_sha256"][:8] in m["version"]
    assert m["calibration"] in {"sigmoid", "isotonic"}
    for bins in m["drift_reference"]["features"].values():
        assert sum(bins["proportions"]) == pytest.approx(1)


def test_verified_model_loads_and_scores(saved, data) -> None:  # type: ignore[no-untyped-def]
    manifest, payload = load_verified(saved.folder)
    p = payload["estimator"].predict_proba(data.splits["test"].X)[:, 1]
    assert manifest["family"] == "logreg" and np.all((p >= 0) & (p <= 1))


def test_tampered_artefact_is_refused(saved) -> None:  # type: ignore[no-untyped-def]
    path = saved.folder / "model.joblib"
    path.write_bytes(path.read_bytes() + b"\x00")
    with pytest.raises(ManifestError, match="hash mismatch"):
        load_verified(saved.folder)


def test_schema_mismatch_is_refused(saved) -> None:  # type: ignore[no-untyped-def]
    mpath = saved.folder / "manifest.json"
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    manifest["feature_schema_hash"] = "0" * 16
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ManifestError, match="schema"):
        verify(saved.folder)


def test_missing_artefact_is_refused(saved) -> None:  # type: ignore[no-untyped-def]
    (saved.folder / "model.joblib").unlink()
    with pytest.raises(ManifestError, match="missing"):
        verify(saved.folder)


def test_rules_manifest_has_no_artefact(tmp_path: Path) -> None:
    sm = save_rules_manifest(
        tmp_path, dataset_version="x", params={"A": {}}, metrics={}, thresholds={},
        changed={}, tuning=[],
    )  # fmt: skip
    assert verify(sm.folder)["family"] == "rules_baseline"
    with pytest.raises(ManifestError, match="no model artefact"):
        load_verified(sm.folder)


def test_isolation_forest_scores_are_ranks(data) -> None:  # type: ignore[no-untyped-def]
    f = candidates.fit_isolation_forest(data.splits["train"], seed=0)
    s = f.score(data.splits["train"].X)
    assert s.min() > 0 and s.max() == 1.0


def test_experiment_runs_end_to_end_and_reproducibly(built: BuiltDataset, tmp_path: Path) -> None:
    kw = {"bootstrap": 20, "seeds": (0, 1)}
    a = run(built.path, tmp_path / "m1", tmp_path / "r1" / "report", **kw)
    b = run(built.path, tmp_path / "m2", tmp_path / "r2" / "report", **kw)
    assert a["selection"] == b["selection"]
    assert a["test"]["random_forest"]["at_budget"] == b["test"]["random_forest"]["at_budget"]
    assert {k: v["version"] for k, v in a["artefacts"].items()} == {
        k: v["version"] for k, v in b["artefacts"].items()
    }
    md = (tmp_path / "r1" / "report.md").read_text(encoding="utf-8")
    assert "Test results (evaluated once)" in md and "synthetic data" in md
    for family in ("logreg", "random_forest", "isolation_forest"):
        verify(Path(a["artefacts"][family]["path"]))
    selected = a["selection"]["selected"]
    assert selected in a["validation"] and selected != "rules_v1"


@pytest.mark.parametrize("family", ["logreg", "random_forest", "isolation_forest"])
def test_backend_loader_accepts_ml_artefacts(data, tmp_path: Path, family: str) -> None:  # type: ignore[no-untyped-def]
    """Contract: what sentinel-ml saves, the application's verified loader can use."""
    from sentinel.inference import load_model

    tr, va = data.splits["train"], data.splits["validation"]
    if family == "isolation_forest":
        fitted = candidates.fit_isolation_forest(tr, seed=0)
    else:
        params = {"C": 0.1} if family == "logreg" else {"max_depth": 4, "min_samples_leaf": 5}
        fitted = candidates.fit_supervised(family, params, tr, va, seed=0)
    sm = save_model(
        fitted, tmp_path, dataset_version=data.version, reference=data.reference.to_dict(),
        drift=drift_reference(tr.X, fitted.score(tr.X)), thresholds={}, metrics={}, selected=False,
    )  # fmt: skip
    model = load_model(
        sm.folder, expected_sha256=sm.manifest["artefact_sha256"], version=sm.manifest["version"]
    )
    rows = data.splits["test"].rows()[:25]
    scores = model.predict(rows)
    expected = fitted.score(data.splits["test"].X.iloc[:25])
    assert np.allclose(scores, expected)  # same numbers in training and serving
    explained = model.explain(rows[:3])
    assert len(explained) == 3
    if family != "isolation_forest":
        assert all(len(e) > 0 for e in explained)
