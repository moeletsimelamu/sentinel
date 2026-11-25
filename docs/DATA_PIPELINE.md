# Sentinel — Synthetic Data & Feature Pipeline

> Phase 1 design. Implementation in Phases 3 (validation) and 5 (generator, features, dataset versioning).

## 1. Why synthetic, and what that means for results

Real transaction data is sensitive and unavailable. Public fraud datasets are either anonymised PCA components (no behavioural meaning) or lack customer history. So Sentinel generates its own data.

**The central honesty problem:** we design the fraud patterns *and* the detectors. A model can score well simply by rediscovering the generator's rules. The generator is therefore built to make that harder, and every report states this limitation:

- Fraud is injected as **noisy episodes**, not clean signatures — amounts, timing and device behaviour are sampled from overlapping distributions.
- **Benign anomalies** are injected on purpose (travel, salary-day spending, new phones, annual purchases) so unusual ≠ fraudulent.
- Some typologies are **deliberately low-signal** (low-and-slow fraud) so false negatives exist and must be analysed.
- **Label noise**: a fraction of injected fraud is left unlabelled, mimicking undetected fraud in real labels.

Results obtained on this data demonstrate methodology only. They do not estimate real-world detection performance.

## 2. Generator design (`ml/sentinel_ml/generate/`)

Seeded (`numpy.random.default_rng(seed)`), deterministic, configurable via a YAML profile. Profiles: `demo` (~2k customers × 90 days ≈ 80k txns, used for the seeded app) and `full` (~6k customers × 180 days ≈ 400k txns, used for training).

### 2.1 Customers
Each synthetic customer gets *behavioural* parameters only:

| Parameter | Distribution |
|---|---|
| Segment (`everyday`, `frequent_spender`, `small_business`, `traveller`, `low_activity`) | Categorical mix |
| Daily transaction rate λ | Gamma, segment-dependent |
| Amount profile | Log-normal μ, σ per merchant category, segment-scaled |
| Preferred merchant categories | Dirichlet weights over ~14 categories |
| Active hours | Mixture of 1–2 Gaussians over hour-of-day |
| Home city | Weighted choice over a fixed list of ~25 cities with coordinates (simulated geography) |
| Devices | 1–3 device ids; small chance of a new device per month |
| Channel mix | card-present / online / mobile app / ATM / transfer |

No names, gender, age, ethnicity, nationality or addresses are generated. Segment is a spending-behaviour archetype, not a demographic.

### 2.2 Legitimate activity
Day-by-day simulation: number of transactions ~ Poisson(λ × weekday factor × payday factor), each with a category from the customer's preferences, amount from the category's log-normal, timestamp from active hours, location = home city (with occasional nearby city), device from the customer's set, status mostly `completed` with a small natural decline rate.

### 2.3 Benign anomalies (false-positive pressure)
| Scenario | Effect |
|---|---|
| Travel trip | Several days in a distant city, new merchants, some hotels/airlines |
| Big-ticket purchase | One amount 5–20× baseline at electronics/furniture |
| New phone | New device id, then consistent use |
| Late-night shift worker | Night-time activity that is normal for that customer |
| Payday splurge | Burst of spending on salary day |

### 2.4 Fraud typologies (injected episodes, ≈0.6–1.0% of transactions)
| Typology | Pattern | Expected difficulty |
|---|---|---|
| `account_takeover` | New device + new city, burst of high-value online/transfer transactions within hours | Medium |
| `card_testing` | Many small online transactions, several declined, then larger ones | Easy for velocity rules |
| `impossible_travel` | Transactions in two cities too far apart for the time elapsed | Easy-medium |
| `high_risk_category_shift` | Sudden spend in gift cards / crypto exchange / money transfer categories | Medium |
| `night_drain` | Several high-value transactions at hours the customer is never active | Medium |
| `mule_passthrough` | Incoming transfer followed quickly by near-equal outgoing transfers | Medium-hard |
| `low_and_slow` | Small, normal-looking amounts at familiar categories, slightly elevated frequency over weeks | **Hard** — intended false negatives |

Each episode records `scenario_id` and `typology` in the label file so evaluation can be broken down per typology.

### 2.5 Labels
- `is_fraud_injected` (ground truth) and `is_fraud_labelled` (observed label: ~5% of injected fraud flipped to 0 to simulate undiscovered fraud).
- Models train on the **observed** label; evaluation reports against both, explaining the gap.

### 2.6 Outputs
`data/<dataset_version>/transactions.parquet`, `labels.parquet`, `customers.parquet`, a CSV export for the ingestion demo (labels excluded), and a **dataset card** (`dataset_card.md`) with generator version, seed, profile, row counts, fraud rate per typology and SHA-256 hashes. `dataset_version` = `<profile>-<generator_version>-<seed>`; identical inputs reproduce identical bytes.

The CSV used for the ingestion demo also contains a small, deliberate set of malformed rows (bad currency, negative amount, missing timestamp, duplicate id) so validation reporting can be demonstrated.

## 3. Ingestion validation (Phase 3 — implemented)

Implemented in `backend/src/sentinel/modules/ingestion/` (pure `validation.py` and `csv_reader.py`, DB-side `pipeline.py`). The exact field rules and every error code are in [DATA_DICTIONARY.md](DATA_DICTIONARY.md). Summary:

| Check | Outcome on failure |
|---|---|
| Required columns present; no card / credential / personal-data / protected-attribute columns; UTF-8; size and row limits | Whole file rejected |
| Types parse (Decimal amount, ISO-8601 timestamp with zone) | Row rejected, field-level error |
| Domain checks (amount > 0, currency in allowed list, enums valid, lat/lon in range, timestamp not in future) | Row rejected |
| Duplicate within file / against DB (`source_system, external_id`) | Row counted as duplicate, not inserted |
| Same content, different id (content hash) | Accepted but flagged as *possible duplicate* in the import summary |
| Unknown customer ref | Customer auto-created (pseudonymous) — documented behaviour |
| Value looks like a card number (Luhn) | Row rejected, value redacted in the report |
| Unknown columns (incl. generator labels) | Ignored, listed in the import summary, never stored |
| Missing optional fields (device, city) | Accepted; stored as null; features treat as explicit "unknown" |

## 4. Feature engineering (`backend/src/sentinel/features/`)

> **Phase 4 status:** the behavioural subset used by detection rules is implemented as `build_feature_rows(history, target_ids)` in **pure Python** (incremental sliding windows per customer, linear time) rather than pandas. Reasons: exact half-open windows are simple to make leakage-proof incrementally, the API/worker image needs no scientific stack yet, and the same function can be called from `ml/` (rows convert directly to a DataFrame). Phase 5 adds the remaining ML features, revisits pandas if training-scale performance requires it, and keeps a parity test between the two paths. Implemented definitions are in [DATA_DICTIONARY.md §5](DATA_DICTIONARY.md).

One pure function, `build_features(history_df, target_ids, config) -> FeatureFrame`. All behavioural aggregates use only transactions **strictly earlier** than the one being scored for the same customer.

### 4.1 Feature catalogue (v1)

| Group | Feature | Definition |
|---|---|---|
| Amount | `log_amount` | log1p(amount) |
| | `amount_vs_cust_median_90d` | amount ÷ customer's prior 90-day median (null → population median) |
| | `amount_zscore_cust_90d` | robust z-score using median/MAD |
| | `amount_vs_category_p95` | amount ÷ population 95th percentile for that category (from training reference) |
| Velocity | `txn_count_1h`, `txn_count_24h` | prior transactions in window |
| | `amount_sum_24h` | prior spend in 24h |
| | `velocity_ratio_24h_vs_30d` | 24h count ÷ average daily count over prior 30d |
| | `failed_count_24h` | prior declined/failed transactions in 24h |
| | `secs_since_prev_txn` | log-scaled time gap |
| Time | `hour_sin`, `hour_cos` | cyclical hour encoding |
| | `hour_rarity_cust` | 1 − share of customer's prior transactions in this hour bucket (±1h) |
| | `is_weekend` | |
| Geography | `km_from_prev_txn` | haversine distance from previous transaction |
| | `implied_speed_kmh` | distance ÷ elapsed hours (capped) |
| | `is_new_city_90d` | city not seen for this customer in prior 90d |
| Device / channel | `is_new_device_90d` | device hash not seen in prior 90d |
| | `channel_rarity_cust` | 1 − customer's prior share of this channel |
| Merchant | `is_new_category_90d` | |
| | `high_risk_category` | category in configured list (gift cards, crypto, money transfer) |
| | `category_share_shift_7d_vs_90d` | change in category mix (Jensen–Shannon distance) |
| History | `customer_tenure_days` | days since first seen |
| | `history_count_90d` | prior transaction count (sparse-history indicator) |

**Excluded on purpose:** customer identifiers, segment (an archetype that leaks the generator's structure), any label-derived feature (e.g. "prior fraud on account"), and any protected attribute (none exist in the data).

### 4.2 Missing values and cold start
New customers have no history. Behavioural ratios fall back to population reference values from the training set, and `history_count_90d` lets the model learn how much to trust them. No row is dropped for missing optional fields; an explicit indicator is used where missingness is informative.

### 4.3 Normalisation and outliers
- Scaling (`StandardScaler` / `RobustScaler`) lives **inside** the sklearn `Pipeline`, fitted on the training split only. Tree models skip scaling.
- Heavy-tailed features are log-transformed and winsorised at training-set percentiles stored in the model manifest.
- Outlier analysis (Phase 5 notebook): distribution plots per feature, per class, to document which outliers are signal versus generator artefacts.

## 5. Leakage controls

| Risk | Control |
|---|---|
| Future data in window features | Windows are `closed="left"` and computed per customer in time order; parity test vs. incremental worker path |
| Random split mixing time periods | **Time-based split**: train = first 60% of the timeline, validation = next 20%, test = last 20% |
| Same episode in train and test | Fraud episodes are assigned wholly to the split containing their first transaction; boundary-crossing episodes dropped from the later split |
| Customer memorisation | Secondary evaluation on a **customer-disjoint** holdout (10% of customers excluded from training entirely) |
| Preprocessing fitted on all data | All fitted transforms inside the sklearn Pipeline; population reference stats computed from train only |
| Threshold tuned on test | Thresholds and model selection decided on validation; test evaluated **once** per final candidate |
| Label used as feature | Feature schema test asserts no label/typology/scenario columns reach `build_features` output |

## 6. Dataset versioning

Every training run records `dataset_version`, the generator config hash, split boundaries, and per-split row/fraud counts in the model manifest and in `model_evaluations`. Datasets are regenerated, not committed — the seed and config are the source of truth.
