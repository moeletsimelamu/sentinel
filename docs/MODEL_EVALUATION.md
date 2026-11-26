# Sentinel — Model Evaluation Methodology

> Phase 1 defines the method. Results are filled in during Phase 6 and must be reported as obtained, including unfavourable ones. All results are on **synthetic data** and say nothing about real-world fraud detection performance.

## 1. Candidates

| # | Approach | Uses labels? | Why it is in the comparison |
|---|---|---|---|
| 0 | **Rule baseline** — configured rules, score = capped weighted sum | No | The bar any model must beat to justify its complexity |
| 1 | **Logistic Regression** (class-weighted, L2, scaled features) | Yes | Strong, calibrated-ish, exactly explainable linear baseline |
| 2 | **Random Forest** (class-weighted or balanced subsample) | Yes | Captures interactions (e.g. new device × high amount) |
| 3 | **Isolation Forest** (fitted on training features without labels) | No | Tests whether unsupervised anomaly detection finds unlabelled/novel patterns |
| 4 | **Hybrid** — final scoring formula (rules + chosen model) | Partly | What the application actually runs |

Gradient boosting (e.g. `HistGradientBoostingClassifier`) may be added if RF underperforms; it will not be added just to have more models.

## 2. Protocol

1. Time-based train / validation / test split, plus customer-disjoint holdout (see DATA_PIPELINE.md §5).
2. Hyperparameters tuned with `TimeSeriesSplit` cross-validation **inside the training period** only; small, documented search grids.
3. Probability calibration for supervised models (`CalibratedClassifierCV`, isotonic or sigmoid chosen on validation) because scores are shown to analysts.
4. Operating threshold selected on **validation** using an alert-budget rule: the threshold that produces at most *B* alerts per 1,000 transactions (default *B* = 10, i.e. what an analyst team can review), reported alongside the F1-optimal threshold for reference.
5. Final candidates evaluated **once** on the test split. Results stored in `model_evaluations` and in this document.
6. Repeat training with 5 seeds; report mean ± std. Bootstrap (1,000 resamples) 95% CIs for PR-AUC and recall on test.

## 3. Metrics

Fraud prevalence is ~1%. A model predicting "never fraud" scores ~99% accuracy, so **accuracy is not reported as a headline metric.**

| Metric | Why |
|---|---|
| **PR-AUC (average precision)** — primary ranking metric | Sensitive to performance on the rare class; baseline equals prevalence |
| ROC-AUC — secondary | Comparable across prevalence, but optimistic under heavy imbalance |
| Precision, Recall, F1 at the operating threshold | What analysts actually experience |
| **Precision@k / Recall at alert budget** | "If we review the top 1% of transactions, what share of fraud do we catch?" |
| False-positive rate & false positives per 1,000 txns | Analyst workload |
| Confusion matrix (counts and rates) | Transparency |
| **Value detection rate** | Share of fraudulent *amount* caught — a missed large fraud matters more than a missed small one |
| Brier score + reliability curve | Calibration of displayed scores |
| Per-typology recall | Which fraud patterns are missed |

Curves stored for the UI: precision–recall, ROC, score histograms by class, reliability curve.

## 4. Error analysis

- **False negatives**: broken down by typology, amount band, customer history length; a sample reviewed manually with feature values and contributions. `low_and_slow` is expected to dominate — if it doesn't, the generator is suspected of being too easy.
- **False positives**: broken down by benign-anomaly scenario (travel, new phone, big purchase). This is the main justification for (or against) adding ML on top of rules.
- **Rule–model disagreement**: transactions caught by one but not the other, to justify the hybrid weighting.
- **Generator leakage check**: if any single feature yields PR-AUC > 0.95 alone, it is investigated as a possible generator artefact before results are accepted.

## 5. Selection criteria (decided before seeing test results)

In priority order, on validation:
1. Recall at the alert budget, then PR-AUC.
2. Value detection rate.
3. Calibration quality (Brier).
4. Explanation quality — can contributions be shown faithfully per transaction?
5. Inference cost on a 50k batch.

A simpler model wins ties. If no ML model beats the rule baseline by a meaningful margin (non-overlapping CIs), that is reported, and rules-only remains the default mode.

## 6. Monitoring after deployment in the app (Phase 9)

| Signal | Method | Interpretation |
|---|---|---|
| Input drift | Population Stability Index per feature vs. training reference bins stored in the manifest (PSI < 0.1 stable, 0.1–0.25 moderate, > 0.25 significant) | The *data* changed — not proof the model got worse |
| Score drift | PSI on score distribution; share of transactions per risk band | Alert volume will change |
| Volume | Predictions/day, alerts/day, alert rate | Operational load |
| Performance | Precision/false-positive trend computed **only** from closed alerts and cases (analyst outcomes), with label delay shown | Actual degradation — available late and biased towards alerted transactions (unalerted transactions rarely get labels) |

The dashboard shows drift and performance in separate panels with this distinction stated in the UI.

## 7. Results (Phase 6)

Dataset `full-1.0.0-42` (379,141 transactions, 0.75% injected fraud in 480 episodes; train 201,619 / validation 72,584 / test 66,791 / holdout 38,046 / excluded 101). Full generated report: [reports/experiment-full-1.0.0-42.md](reports/experiment-full-1.0.0-42.md) (JSON with curves alongside). Reproduce with `sentinel-ml experiment --profile full --seed 42`; a test asserts two runs give identical metrics and artefact hashes.

### Test split (evaluated once, thresholds from validation, budget 10 alerts / 1,000)

| Candidate | PR-AUC [95% CI] | Recall @ budget [95% CI] | Precision | Alerts / 1k | Value detected |
|---|---|---|--:|--:|--:|
| Rules v1 (defaults) | 0.112 [0.082, 0.143] | 18.5% [14.9%, 22.1%] | 27.7% | 4.5 | 34.1% |
| Rules tuned | 0.109 [0.079, 0.141] | 21.9% [17.7%, 25.6%] | 20.7% | 7.2 | 38.7% |
| Logistic regression | 0.356 [0.306, 0.403] | 33.6% [29.1%, 37.8%] | 53.1% | 4.3 | 36.2% |
| **Random forest (selected)** | **0.565** [0.514, 0.612] | **64.5%** [59.8%, 68.8%] | 49.7% | 8.8 | 72.9% |
| Isolation forest | 0.367 [0.319, 0.418] | 43.5% [39.0%, 48.3%] | 34.4% | 8.6 | 62.2% |
| Hybrid (0.25 rules + 0.75 RF) | 0.552 [0.499, 0.602] | 62.5% [58.0%, 67.0%] | 52.5% | 8.1 | 74.6% |

### What the results say

1. **ML clearly beats the rules** on this data: the random forest's recall at the same alert budget is about three times the rule baseline's, with non-overlapping intervals. By the pre-registered criteria (§5) adding ML is justified.
2. **Random forest was selected on validation** (recall 68.3% vs hybrid 66.6%). On test the hybrid is statistically indistinguishable from it, so the simpler candidate stands. Phase 7 still runs rules alongside the model in the application, because rules provide readable reasons and catch patterns the model misses (below).
3. **Impossible travel is the model's blind spot** (12.5% recall vs 25% for rules). The rules-vs-model table shows 17 frauds caught only by rules. The `IMPOSSIBLE_TRAVEL` rule keeps `force_alert`.
4. **Low-and-slow fraud** is the largest share of missed fraud (79 of 176 misses), as designed; rules catch none of it.
5. **Benign anomalies:** the forest flags 1.3% of legitimate travel transactions versus 4.1% for the rules — the false-positive reduction ML is meant to provide.
6. **Isolation forest does not generalise to new customers** (holdout PR-AUC 0.096 vs 0.367 on test, and 15.6 alerts / 1,000 against a budget of 10): anomaly scores are relative to the training population and new customers look anomalous simply for being new.
7. **Logistic regression is weak** (PR-AUC 0.356): the useful signal is in interactions (new device × amount, inflow × outflow) that a linear model cannot express without hand-built crosses.
8. **Stable across seeds:** random forest test PR-AUC 0.563 ± 0.003 over 5 seeds.
9. **Leakage check passed:** the best single input reaches PR-AUC 0.171 on its own (prevalence 0.007); nothing approaches 0.95.
10. **Card testing at 100% recall** is a warning sign that this typology is too easy in the generator; it is flagged in the model card rather than celebrated.

### Rule recalibration

Coordinate search on validation recommended one change: `BEHAVIOUR_SHIFT.min_shift` 0.5 → 0.6. The log shows the deeper problem: at 0.6 the rule still fires on 30% of validation transactions, and at 0.9 (0.9% hit rate) recall at budget falls. The rule's design (comparing a few days' category mix with 90 days) carries almost no signal at any threshold; Phase 7 reviews whether to deactivate it rather than only re-tune it.

### In-application check (Phase 7)

The application was configured with the validated hybrid: random forest activated, scoring config v2 (`w_rule` 0.25, `w_model` 0.75, alert threshold 14, the hybrid's budget threshold on validation), and `BEHAVIOUR_SHIFT` v2 (`min_shift` 0.6). Re-scoring the separately generated **demo** dataset (`demo-1.0.0-42` — same generator, different seed, never used for training) through the worker gave:

| | Value |
|---|--:|
| Alertable transactions | 864 (8.9 per 1,000) → 658 alerts after 24 h de-duplication |
| Precision against injected fraud | 44.6% |
| Recall of injected fraud | 59.0% (652 fraud transactions) |
| Recall by typology | card testing 96%, mule pass-through 75%, night drain 75%, account takeover 53%, low-and-slow 45%, high-risk shift 43%, impossible travel 32% |
| False positives that were benign travel | 56 of 478 |

These are consistent with the offline test results for the hybrid (precision 52.5%, recall 62.5%), and impossible-travel recall is higher than the forest alone (12.5%) because the rule still forces alerts.

### Deviations from the protocol (§2)

- **Calibration:** the validation period was split in time — calibrators fitted on the first half, sigmoid vs isotonic chosen by Brier score on the second half (isotonic won for both models); thresholds were then set on all of validation. Validation numbers are therefore slightly optimistic; test is unaffected.
- **Seeds:** logistic regression uses a deterministic solver, so one fit is reported; random and isolation forests use 5 seeds.
- **Isolation forest** has no labels to tune on; fixed parameters (200 trees, 4,096 samples) are reported as-is.
- **Bootstrap** resamples transactions, not episodes; intervals are optimistic for clustered fraud.
- **Gradient boosting** was not added: the random forest did not underperform, and §1 says it would not be added just to have more models.
