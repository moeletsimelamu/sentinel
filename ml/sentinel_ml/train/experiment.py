"""End-to-end experiment: ``sentinel-ml experiment --profile full --seed 42``.

Order matters and is fixed in code so it cannot drift:
1. leakage check on training features;
2. candidates fitted/tuned with training data only, calibrated and thresholded on
   validation;
3. the selected candidate is decided **on validation** before any test metric exists;
4. every final candidate is evaluated **once** on test (with bootstrap CIs) and on the
   customer-disjoint holdout;
5. error analysis, artefacts, report.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from sentinel.features import FEATURE_SCHEMA_HASH, MODEL_FEATURES

from sentinel_ml.evaluate import metrics
from sentinel_ml.evaluate.report import render_report
from sentinel_ml.train import candidates, rules_baseline
from sentinel_ml.train.artefacts import drift_reference, save_model, save_rules_manifest
from sentinel_ml.train.data import Dataset, Split, load_dataset

SEEDS = (0, 1, 2, 3, 4)
HYBRID_WEIGHTS = (0.25, 0.5, 0.75)  # w_rule; w_model = 1 - w_rule
AMOUNT_BANDS = [(0, 500), (500, 5_000), (5_000, 20_000), (20_000, float("inf"))]
HISTORY_BANDS = [(0, 0), (1, 10), (10, 50), (50, float("inf"))]


@dataclass
class Candidate:
    name: str
    family: str
    score: Callable[[Split], np.ndarray]
    probabilities: bool
    fitted: candidates.Fitted | None = None
    thresholds: dict[str, float] | None = None


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)  # noqa: T201


def _round(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 6)
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_round(v) for v in obj]
    if isinstance(obj, np.generic):
        return _round(obj.item())
    return obj


def _bands(values: np.ndarray, bands: list[tuple[float, float]]) -> np.ndarray:
    """Label each value with its band: ``lo == hi`` exact, else ``[lo, hi)``."""
    out = np.full(len(values), "", dtype=object)
    for lo, hi in bands:
        if lo == hi:
            mask, label = values == lo, f"{lo:g}"
        elif hi == float("inf"):
            mask, label = values >= lo, f"{lo:g}+"
        else:
            mask, label = (values >= lo) & (values < hi), f"{lo:g}-{hi:g}"
        out[mask] = label
    return out


def leakage_check(train: Split) -> dict[str, Any]:
    rows = []
    for col in train.X.columns:
        x = train.X[col].to_numpy()
        ap = max(metrics.average_precision(x, train.y), metrics.average_precision(-x, train.y))
        auc = metrics.roc_auc(x, train.y)
        rows.append({"feature": col, "pr_auc": ap, "roc_auc": max(auc, 1 - auc)})
    rows.sort(key=lambda r: -r["pr_auc"])
    return {
        "prevalence": float(train.y.mean()),
        "top": rows[:10],
        "suspicious": [r["feature"] for r in rows if r["pr_auc"] > 0.95],
    }


def thresholds_for(scores: np.ndarray, y: np.ndarray, budget: float) -> dict[str, float]:
    return {
        "budget": metrics.budget_threshold(scores, budget),
        "f1": metrics.f1_optimal_threshold(scores, y),
    }


def error_analysis(scores: np.ndarray, split: Split, threshold: float) -> dict[str, Any]:
    alerted = scores >= threshold
    y = split.y_true  # analyse against ground truth, including unlabelled fraud
    fn, fp = y & ~alerted, ~y & alerted
    amount_band = _bands(split.amount_zar, AMOUNT_BANDS)
    history_band = _bands(split.history.astype(float), HISTORY_BANDS)

    def breakdown(mask: np.ndarray, groups: np.ndarray, base: np.ndarray) -> dict[str, Any]:
        out = {}
        for g in sorted({str(v) for v in groups[base]}):
            in_group = base & (groups.astype(str) == g)
            out[g or "ordinary"] = {
                "total": int(in_group.sum()),
                "count": int((mask & in_group).sum()),
            }
        return out

    benign = np.where(split.benign == "", "ordinary", split.benign)
    legit = ~y
    return {
        "recall_by_typology": metrics.group_recall(scores, y, split.typology, threshold),
        "false_negatives_by_amount_zar": breakdown(fn, amount_band, y),
        "false_negatives_by_history": breakdown(fn, history_band, y),
        "false_positives_by_benign_scenario": {
            k: {**v, "fp_rate": v["count"] / v["total"] if v["total"] else 0.0}
            for k, v in breakdown(fp, benign, legit).items()
        },
        "unlabelled_fraud_caught": int((split.y_true & ~split.y & alerted).sum()),
        "unlabelled_fraud_total": int((split.y_true & ~split.y).sum()),
    }


def disagreement(a: np.ndarray, b: np.ndarray, y: np.ndarray) -> dict[str, dict[str, int]]:
    out = {}
    for label, mask in (("fraud", y), ("legitimate", ~y)):
        out[label] = {
            "both": int((a & b & mask).sum()),
            "rules_only": int((a & ~b & mask).sum()),
            "model_only": int((~a & b & mask).sum()),
            "neither": int((~a & ~b & mask).sum()),
        }
    return out


def run(
    dataset_folder: Path,
    models_root: Path,
    report_path: Path,
    *,
    budget: float = metrics.DEFAULT_BUDGET_PER_1000,
    seeds: tuple[int, ...] = SEEDS,
    bootstrap: int = 1000,
) -> dict[str, Any]:
    started = time.perf_counter()
    _log(f"loading {dataset_folder}")
    ds: Dataset = load_dataset(dataset_folder)
    tr, va, te, ho = (ds.splits[s] for s in ("train", "validation", "test", "holdout"))
    _log(f"train {len(tr):,} / validation {len(va):,} / test {len(te):,} / holdout {len(ho):,}")

    report: dict[str, Any] = {
        "dataset_version": ds.version,
        "feature_schema_hash": FEATURE_SCHEMA_HASH,
        "n_features": len(MODEL_FEATURES),
        "budget_per_1000": budget,
        "seeds": list(seeds),
        "splits": {
            s.name: {
                "n": len(s),
                "labelled_fraud": int(s.y.sum()),
                "injected_fraud": int(s.y_true.sum()),
            }
            for s in (tr, va, te, ho)
        },
    }

    _log("leakage check")
    report["leakage_check"] = leakage_check(tr)

    # --- candidate 0: rules (v1 defaults and validation-tuned) --------------------------
    _log("rule baseline")
    rows = {s.name: s.rows() for s in (va, te, ho)}
    default_cfg = rules_baseline.RuleConfig()
    tuned_cfg, tuning_log = rules_baseline.tune(rows["validation"], va.y, budget)
    rule_cache: dict[tuple[str, str], np.ndarray] = {}

    def rule_scorer(cfg: rules_baseline.RuleConfig, tag: str) -> Callable[[Split], np.ndarray]:
        def _score(split: Split) -> np.ndarray:
            key = (tag, split.name)
            if key not in rule_cache:
                hits = rules_baseline.hit_matrix(rows[split.name], cfg)
                rule_cache[key] = rules_baseline.score(hits, cfg)
            return rule_cache[key]

        return _score

    cands: list[Candidate] = [
        Candidate("rules_v1", "rules_baseline", rule_scorer(default_cfg, "v1"), False),
        Candidate("rules_tuned", "rules_baseline", rule_scorer(tuned_cfg, "tuned"), False),
    ]
    report["rule_tuning"] = {
        "grid": rules_baseline.TUNING_GRID,
        "recommended_changes": rules_baseline.changed_params(tuned_cfg),
        "log": tuning_log,
        "hit_rates_validation_v1": {
            c: float(h.mean())
            for c, h in rules_baseline.hit_matrix(rows["validation"], default_cfg).items()
        },
    }

    # --- candidates 1-3: models -----------------------------------------------------------
    fitted: dict[str, candidates.Fitted] = {}
    tuning: dict[str, Any] = {}
    for family in ("logreg", "random_forest"):
        _log(f"tuning {family} (TimeSeriesSplit inside training)")
        params, log = candidates.tune(family, tr, seed=0)
        tuning[family] = {"selected": params, "log": log}
        _log(f"fitting {family} {params}")
        fitted[family] = candidates.fit_supervised(family, params, tr, va, seed=0)
    _log("fitting isolation_forest")
    fitted["isolation_forest"] = candidates.fit_isolation_forest(tr, seed=0)
    report["hyperparameter_tuning"] = tuning

    for family, f in fitted.items():
        cands.append(
            Candidate(
                family, family, lambda s, f=f: f.score(s.X), f.family != "isolation_forest", f
            )
        )

    # --- thresholds and validation metrics -------------------------------------------------
    val_scores = {c.name: c.score(va) for c in cands}
    for c in cands:
        c.thresholds = thresholds_for(val_scores[c.name], va.y, budget)

    # --- candidate 4: hybrid (tuned rules + best supervised model), weights on validation ----
    def val_objective(scores: np.ndarray) -> tuple[float, float]:
        return rules_baseline.objective(scores, va.y, budget)

    best_model = max(("logreg", "random_forest"), key=lambda n: val_objective(val_scores[n]))
    hybrid_log = []
    best_w, best_obj = HYBRID_WEIGHTS[0], (-1.0, -1.0)
    model_f = fitted[best_model]
    for w in HYBRID_WEIGHTS:
        s = w * val_scores["rules_tuned"] + (1 - w) * 100 * val_scores[best_model]
        obj = val_objective(s)
        hybrid_log.append({"w_rule": w, "recall_at_budget": obj[0], "pr_auc": obj[1]})
        if obj > best_obj:
            best_w, best_obj = w, obj
    rules_tuned_score = cands[1].score

    def hybrid_score(split: Split, w: float = best_w) -> np.ndarray:
        return w * rules_tuned_score(split) + (1 - w) * 100 * model_f.score(split.X)

    hybrid = Candidate("hybrid", "hybrid", hybrid_score, False)
    val_scores["hybrid"] = hybrid_score(va)
    hybrid.thresholds = thresholds_for(val_scores["hybrid"], va.y, budget)
    cands.append(hybrid)
    report["hybrid"] = {
        "model": best_model,
        "w_rule": best_w,
        "w_model": 1 - best_w,
        "log": hybrid_log,
    }

    validation = {}
    for c in cands:
        assert c.thresholds is not None  # noqa: S101
        validation[c.name] = {
            "at_budget": metrics.evaluate(
                val_scores[c.name], va.y, threshold=c.thresholds["budget"], amounts=va.amount_zar,
                probabilities=c.probabilities,
            ),
            "at_f1": metrics.evaluate(val_scores[c.name], va.y, threshold=c.thresholds["f1"]),
        }  # fmt: skip
    report["validation"] = validation

    # --- selection, decided on validation before any test metric is computed ---------------
    ranked = sorted(
        (c.name for c in cands if c.name != "rules_v1"),
        key=lambda n: (
            validation[n]["at_budget"]["recall"],
            validation[n]["at_budget"]["pr_auc"],
        ),
        reverse=True,
    )
    selected = ranked[0]
    report["selection"] = {"ranking_on_validation": ranked, "selected": selected}
    _log(f"selected on validation: {selected}")

    # --- seed variance (validation and test) ---------------------------------------------
    _log("seed variance")
    variance: dict[str, Any] = {}
    for family in ("random_forest", "isolation_forest", "logreg"):
        per_seed = []
        for seed in seeds if family != "logreg" else (0,):
            f = fitted[family] if seed == 0 else candidates.refit_like(fitted[family], tr, va, seed)
            sv, st = f.score(va.X), f.score(te.X)
            thr = metrics.budget_threshold(sv, budget)
            per_seed.append(
                {
                    "seed": seed,
                    "validation_pr_auc": metrics.average_precision(sv, va.y),
                    "test_pr_auc": metrics.average_precision(st, te.y),
                    "test_recall_at_budget": metrics.confusion(st, te.y, thr).recall,
                }
            )
        summary = {}
        for key in ("validation_pr_auc", "test_pr_auc", "test_recall_at_budget"):
            vals = np.array([p[key] for p in per_seed])
            summary[key] = {"mean": float(vals.mean()), "std": float(vals.std())}
        note = "deterministic solver; one fit" if family == "logreg" else None
        variance[family] = {"per_seed": per_seed, "summary": summary, "note": note}
    report["seed_variance"] = variance

    # --- test (once) and holdout -------------------------------------------------------------
    _log("test and holdout evaluation")
    test, holdout = {}, {}
    test_scores = {}
    for c in cands:
        assert c.thresholds is not None  # noqa: S101
        st = c.score(te)
        test_scores[c.name] = st
        thr = c.thresholds["budget"]
        test[c.name] = {
            "at_budget": metrics.evaluate(st, te.y, threshold=thr, amounts=te.amount_zar,
                                          probabilities=c.probabilities),
            "at_f1": metrics.evaluate(st, te.y, threshold=c.thresholds["f1"]),
            "against_injected_truth": metrics.evaluate(st, te.y_true, threshold=thr),
            "ci95": metrics.bootstrap_ci(st, te.y, thr, resamples=bootstrap, seed=0),
            "curves": metrics.curves(st, te.y),
        }  # fmt: skip
        sh = c.score(ho)
        holdout[c.name] = metrics.evaluate(sh, ho.y, threshold=thr, amounts=ho.amount_zar)
    report["test"] = test
    report["holdout"] = holdout

    budget_thr = {c.name: (c.thresholds or {})["budget"] for c in cands}
    report["error_analysis"] = {
        name: error_analysis(test_scores[name], te, budget_thr[name])
        for name in dict.fromkeys([selected, "rules_tuned", best_model])
    }
    report["rule_model_disagreement"] = {
        "model": best_model,
        "test": disagreement(
            test_scores["rules_tuned"] >= budget_thr["rules_tuned"],
            test_scores[best_model] >= budget_thr[best_model],
            te.y_true,
        ),
    }

    # --- artefacts ----------------------------------------------------------------------
    _log("saving artefacts")
    saved = {}
    model_component = best_model if selected == "hybrid" else selected
    by_name = {c.name: c for c in cands}
    for family, f in fitted.items():
        c = by_name[family]
        sm = save_model(
            f,
            models_root,
            dataset_version=ds.version,
            reference=ds.reference.to_dict(),
            drift=drift_reference(tr.X, f.score(tr.X)),
            thresholds=c.thresholds or {},
            metrics=_round(
                {
                    "validation": validation[family],
                    "test": test[family],
                    "holdout": holdout[family],
                }
            ),
            selected=family == model_component,
            extra={"recommended_scoring": report["hybrid"] if family == best_model else None},
        )
        saved[family] = {"version": sm.manifest["version"], "path": str(sm.folder.as_posix())}
    rules_c = by_name["rules_tuned"]
    rm = save_rules_manifest(
        models_root,
        dataset_version=ds.version,
        params=tuned_cfg.params,
        metrics=_round(
            {
                "validation": validation["rules_tuned"],
                "test": test["rules_tuned"],
            }
        ),
        thresholds=rules_c.thresholds or {},
        changed=rules_baseline.changed_params(tuned_cfg),
        tuning=tuning_log,
    )
    saved["rules_baseline"] = {"version": rm.manifest["version"], "path": str(rm.folder.as_posix())}
    report["artefacts"] = saved
    report["runtime_seconds"] = round(time.perf_counter() - started, 1)

    report = _round(report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    (report_path.parent / f"{report_path.name}.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (report_path.parent / f"{report_path.name}.md").write_text(
        render_report(report), encoding="utf-8"
    )
    _log(f"done in {report['runtime_seconds']}s -> {report_path.name}.md")
    return report
