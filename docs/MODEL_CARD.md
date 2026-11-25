# Model card — `random_forest-full-1.0.0-42-s0-6f318a3f`

> Trained and evaluated on **synthetic data** produced by Sentinel's own generator. The numbers below describe behaviour on that data and do not estimate real-world fraud detection performance. A score is an investigative indicator for a human reviewer, never a finding of fraud.

## Model details

| | |
|---|---|
| Family | Random forest classifier (scikit-learn 1.9.1), isotonic-calibrated |
| Parameters | 150 trees, `max_depth=16`, `min_samples_leaf=25`, `max_features="sqrt"`, `class_weight="balanced_subsample"`, `random_state=0` — selected by `TimeSeriesSplit` (3 folds) inside the training period |
| Inputs | 33 numeric behavioural features, schema `f5fe5967a74a6ef1` (`sentinel.features.MODEL_FEATURES`, documented in [DATA_DICTIONARY.md §6](DATA_DICTIONARY.md)) |
| Output | Calibrated score 0–1 per transaction |
| Operating threshold | 0.085 — chosen on validation so that at most 10 transactions per 1,000 are alerted |
| Training data | `full-1.0.0-42`: 201,619 training transactions (1,400 labelled fraud) |
| Artefact | `models/random_forest/<version>/model.joblib`, SHA-256 in `manifest.json`; verified before every load |
| Reproduce | `cd ml; .\.venv\Scripts\sentinel-ml.exe experiment --profile full --seed 42` (≈ 20 min on a laptop; re-running reproduces the same artefact hash) |

## Intended use

- Ranking transactions for **analyst review** inside Sentinel, next to the rule engine.
- Demonstrating an honest, leakage-controlled evaluation methodology in a portfolio project.

## Not intended for

- Any real customer, card or bank data, or any automated decision (blocking, declining, reporting a customer) without a human investigation.
- Inferring anything about a person. The model has no personal or protected attributes and must not be extended with them.

## Inputs deliberately excluded

Customer and transaction identifiers, the generator's spending `segment`, labels and anything derived from them (`typology`, `scenario_id`), and protected attributes — none exist in the data, and a test asserts none reach the model.

## Performance (test split, evaluated once; 95% bootstrap CIs)

| Metric | Random forest | Rule baseline (tuned) |
|---|--:|--:|
| PR-AUC | **0.565** [0.514, 0.612] | 0.109 [0.079, 0.141] |
| Recall at 10 alerts / 1,000 | **64.5%** [59.8%, 68.8%] | 21.9% [17.7%, 25.6%] |
| Precision at that threshold | 49.7% | 20.7% |
| Alerts per 1,000 (budget 10) | 8.8 | 7.2 |
| False positives per 1,000 | 4.4 | 5.7 |
| Fraudulent value detected | 72.9% | 38.7% |
| Brier score | 0.0039 | — |
| Customer-disjoint holdout PR-AUC | 0.643 | 0.042 |

Seed variance (5 seeds): test PR-AUC 0.563 ± 0.003, recall at budget 63.5% ± 0.9%.

### By fraud typology (test, recall at the operating threshold)

| Typology | Recall | Comment |
|---|--:|---|
| `card_testing` | 100% | Probably too easy: tiny declined probes are a near-unique signature in the generator |
| `night_drain` | 83% | |
| `mule_passthrough` | 79% | Needs the 24 h inflow features added in Phase 5 |
| `account_takeover` | 67% | Only the first transaction of an episode is on a "new" device |
| `high_risk_category_shift` | 52% | |
| `low_and_slow` | 37% | Designed to be hard; still the largest share of missed fraud (45%) |
| `impossible_travel` | **12.5%** | Worse than the rules (25%); the `IMPOSSIBLE_TRAVEL` rule (`force_alert`) must stay active alongside the model |

## Known limitations

- **Synthetic data.** The generator and the detectors were designed by the same author. Despite noisy episodes, benign look-alikes and label noise, some patterns (card testing) are easier than they would be in reality.
- **Labels are partial by design.** The model learns from observed labels (some fraud deliberately unlabelled). It still caught 31 of 46 unlabelled fraud transactions on test, but real labels are biased towards what was already investigated.
- **Confidence intervals are optimistic.** Bootstrap resampling is by transaction; fraud arrives in episodes, so a cluster bootstrap would give wider intervals.
- **Calibration** was fitted on the first half of the validation period; displayed probabilities are only as reliable as that small sample (≈ 270 positives).
- **Explanations.** Random forests have no exact additive explanation. Phase 7 shows path-based (Saabas-style) feature contributions, which are approximate; rule hits remain the primary human-readable reasons.
- **Drift.** Behaviour, merchants and fraud change. Training-time feature bins are stored in the manifest for PSI monitoring (Phase 9); a change in input distribution is not by itself proof of degraded performance.

## Ethical considerations

The model ranks *transactions* for review; it does not label customers. Alerts are worded as "flagged for review", and only an analyst's closed case may record a confirmed outcome. No demographic or protected attribute is generated, stored or used, so fairness across such groups cannot be measured here — a real deployment would need that analysis on real, lawfully obtained data.
