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

## 7. Results

_To be completed in Phase 6._
