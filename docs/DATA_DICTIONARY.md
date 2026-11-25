# Sentinel — Data Dictionary

> Status: Phase 5 (ingestion, behavioural features `behaviour-v2`, model inputs). Model outputs are added in Phases 6–7.
> All data is synthetic. Sentinel never accepts real card numbers, bank credentials or personal data.

## 1. Transaction import format

The same fields are used by CSV upload (`POST /api/v1/imports`) and REST ingestion (`POST /api/v1/transactions`). CSV headers are case-insensitive; spaces and punctuation are normalised to `_` (so `External ID` = `external_id`).

| Field | Required | Type / format | Rules |
|---|:-:|---|---|
| `external_id` | ✅ | 1–64 chars: letters, digits, `_ - . :`; starts with a letter or digit | Unique per `source_system`. A repeat is a **duplicate** and is not stored again |
| `customer_ref` | ✅ | Same as `external_id` | Pseudonymous id. Unknown refs create a new customer record (no personal data) |
| `occurred_at` | ✅ | ISO 8601 **with UTC offset**, e.g. `2026-03-01T09:15:00Z`, `2026-03-01T11:15:00+02:00` | Stored in UTC. Not before 2000-01-01; not more than 5 minutes in the future |
| `amount` | ✅ | Decimal string, e.g. `125.50` | > 0, ≤ 10,000,000.00, at most 2 decimal places. REST clients should send strings; JSON numbers are accepted |
| `currency` | ✅ | ISO 4217 | One of `ZAR`, `USD`, `EUR`, `GBP` (case-insensitive) |
| `txn_type` | ✅ | enum | `purchase`, `atm_withdrawal`, `transfer_in`, `transfer_out`, `refund` |
| `channel` | ✅ | enum | `card_present`, `online`, `mobile_app`, `atm`, `bank_transfer` |
| `merchant_category` | ✅ | enum | `groceries`, `restaurants`, `fuel`, `retail_clothing`, `electronics`, `home_furniture`, `travel_airline`, `travel_hotel`, `entertainment`, `utilities`, `health_pharmacy`, `online_marketplace`, `subscriptions`, `gift_cards`¹, `crypto_exchange`¹, `money_transfer`¹, `atm_cash`, `payroll` |
| `status` | ✅ | enum | `completed`, `declined`, `failed` |
| `city` | | text ≤ 64 | Simulated geography |
| `country` | | ISO 3166-1 alpha-2 | e.g. `ZA` |
| `lat`, `lon` | | decimal degrees | Both or neither; lat −90…90, lon −180…180 |
| `device_id` | | text ≤ 128 | Stored only as a keyed hash (HMAC-SHA256); the raw value is never stored |

¹ High-risk categories, used by detection rules (Phase 4).

**Other columns** are ignored and listed in the import summary as `ignored_columns`. This includes label columns from the generator (`is_fraud`, `typology`, …): ground truth never enters the scoring path.

**Forbidden columns** cause the whole file (or REST record) to be rejected: card data (`card_number`, `pan`, `cvv`, `expiry`…), bank credentials (`iban`, `account_number`, `pin`, `password`…), personal data (`name`, `first_name`, `customer_name`, `email`, `phone`, `address`, `date_of_birth`, `id_number`, `passport`…) and protected attributes (`gender`, `race`, `ethnicity`, `nationality`, `religion`, `marital_status`).

### Limits

| Limit | Default | Setting |
|---|---|---|
| CSV file size | 64 MB | `SENTINEL_IMPORT_MAX_BYTES` |
| CSV data rows | 250,000 | `SENTINEL_IMPORT_MAX_ROWS` |
| REST records per request | 1,000 | `SENTINEL_API_INGEST_MAX_RECORDS` |
| Ingestion requests | 30/minute per client | `SENTINEL_INGESTION_RATE_LIMIT` |

## 2. Validation outcomes

### File-level (whole CSV rejected; batch status `rejected`)

| Code | Meaning |
|---|---|
| `empty_file` / `no_rows` | Nothing to import |
| `invalid_encoding` | Not UTF-8 (a UTF-8 BOM is fine) |
| `malformed_csv` | Unterminated quotes, a value over 4,096 characters, or similar |
| `missing_columns` | A required column is absent (`details.columns` lists them) |
| `duplicate_columns` | Two headers normalise to the same name |
| `forbidden_columns` | Card, credential or personal-data columns present |
| `too_many_columns` / `too_many_rows` | Over the limits above |

### Row-level (`GET /api/v1/imports/{id}/issues`)

Each issue has `row_number` (CSV: the line in the file, header = line 1; REST: 1-based position), an `outcome` and one or more `reasons` (`field`, `code`, `message`).

| Outcome | Stored? | Codes |
|---|:-:|---|
| `rejected` | No | `required`, `invalid_type`, `too_long`, `invalid_characters`, `invalid_format`, `invalid_choice`, `invalid_number`, `not_positive`, `too_large`, `too_many_decimals`, `invalid_datetime`, `missing_timezone`, `too_old`, `in_future`, `out_of_range`, `incomplete_coordinates`, `malformed_row` (wrong number of cells), `forbidden_field` (REST), `sensitive_data_detected` (a value looks like a card number — see below) |
| `duplicate` | No | `duplicate_in_file` (id repeated in the same submission), `already_ingested` (id already stored for this source system) |
| `possible_duplicate` | **Yes** | `same_content_in_file`, `same_content_as_existing` — identical details under a different id. Stored, but flagged for a person to review (`related_transaction_id` points to the match) |

Rejected rows keep a copy of the submitted values (`raw_row`) for the report, with unknown columns dropped, `device_id` masked and any card-like value replaced with `[REDACTED]`.

**Card-number detection:** any text field except `device_id` whose value is 13–19 digits (spaces/dashes allowed), starts with 2–6 and passes the Luhn check is rejected. Purely numeric ids of that shape are therefore refused; use prefixed ids such as `TX-000123`.

## 3. Stored entities

| Entity | Notes |
|---|---|
| `customers` | `external_ref` only — no name, DOB, gender, ethnicity, nationality or address. The generator's spending "segment" is deliberately not stored |
| `transactions` | Immutable (database trigger). `content_hash` = SHA-256 of every field except the id, used for possible-duplicate detection |
| `import_batches` | One per upload or REST request: status, counts, ignored columns, file error, SHA-256, uploader. `identical_to_batch_id` is set when the same file was imported before |
| `import_row_issues` | The rejected-record report |
| `import_files` | Raw upload bytes, held only until processing finishes, then deleted |
| `processing_jobs` | Background work queue |

## 4. Import lifecycle

```
CSV upload ─▶ queued ─▶ processing ─┬─▶ completed   (some rows may be rejected)
                                    ├─▶ rejected    (file unusable)
                                    └─▶ failed      (processing error after 3 attempts)
REST request ───────────────────────▶ completed     (synchronous)
```

## 5. Behavioural features (`behaviour-v2`)

Computed by `sentinel.features.build_feature_rows` for every scored transaction and stored with each risk assessment (`risk_assessments.features`). Windows are `[T − d, T)` over the **same customer's strictly earlier** transactions; `null` means *unknown* (usually: no history yet).

| Feature | Definition |
|---|---|
| `amount`, `currency` | As ingested |
| `amount_zar` | Amount × fixed illustrative rate (ZAR 1, USD 18, EUR 19.5, GBP 23) |
| `hour_utc`, `day_of_week` | Hour and weekday (Monday = 0) of `occurred_at`, always in UTC |
| `channel`, `txn_type` | As ingested |
| `merchant_category`, `high_risk_category` | Category; whether it is `gift_cards`, `crypto_exchange` or `money_transfer` |
| `history_count_90d` | Prior transactions in 90 days (any status) |
| `history_span_days_90d` | Days since the earliest prior transaction in the 90-day window |
| `secs_since_prev_txn` | Seconds since the previous transaction (within 90 days) |
| `cust_median_amount_zar_90d` | Median `amount_zar` of prior **completed** transactions in 90 days |
| `amount_vs_cust_median_90d` | `amount_zar` ÷ that median |
| `txn_count_1h`, `txn_count_24h`, `txn_count_30d` | Prior transactions in each window (any status) |
| `velocity_ratio_24h_vs_30d` | `txn_count_24h` ÷ (`txn_count_30d` ÷ 30) |
| `failed_count_24h` | Prior `declined`/`failed` transactions in 24 h |
| `same_category_count_10m` | Prior transactions in the same category within 10 min |
| `spend_sum_24h_zar`, `inflow_sum_24h_zar` | Prior **completed** outgoing amounts / incoming transfers and refunds in 24 h |
| `km_from_prev_txn`, `implied_speed_kmh` | Haversine distance from the previous transaction that had coordinates; ÷ elapsed hours (at least 1 minute; capped at 20,000 km/h) |
| `is_new_city_90d`, `is_new_device_90d`, `is_new_category_90d` | Not seen for this customer in the prior 90 days (`null` when the value or the history is missing) |
| `channel_rarity_cust` | 1 − share of prior-90-day transactions on this channel |
| `hour_rarity_cust` | 1 − share of prior-90-day transactions within ±1 hour of this hour |
| `category_share_shift_7d_vs_90d` | Jensen–Shannon distance (0–1) between the category mix of the last 7 days (including this transaction) and the prior 90 days |

`behaviour-v1` (Phase 4) is the same table without `day_of_week`, `channel`, `txn_type`, `history_span_days_90d`, `secs_since_prev_txn`, the 24 h sums and `channel_rarity_cust`. Each assessment records the feature-set version it used.

## 6. Model inputs (`sentinel.features.MODEL_FEATURES`)

`to_model_vector(row, reference)` turns a feature row into 33 floats in a fixed order identified by `FEATURE_SCHEMA_HASH` (recorded in each dataset card and, from Phase 6, each model manifest). Encoding rules:

- Heavy-tailed values are log-transformed (`log_amount_zar`, `log_amount_vs_cust_median_90d`, velocity ratio, 24 h sums, distance, speed, seconds since previous).
- Unknowns are explicit: "is new" flags are 1 / 0 / -1 (unknown); rarities, shift, distance and speed are -1 when unknown; `has_history` tells a model how far to trust behavioural ratios.
- Cold start: with no customer median, amounts are compared with the **training-split** population median (`feature_reference.json`); `amount_vs_category_p95` uses training-split category 95th percentiles.
- Hour is encoded as `hour_sin` / `hour_cos`; channel is one-hot; `is_transfer_out` and `outflow_vs_inflow_24h` describe pass-through behaviour.

Never model inputs (tested): labels (`is_fraud*`, `typology`, `scenario_id`), the generator's `segment`, customer or transaction identifiers, and protected attributes (none exist).

## 7. Generated datasets (evaluation files)

| File | Contents | Used by |
|---|---|---|
| `labels.parquet` | `is_fraud_injected` (ground truth), `is_fraud_labelled` (observed; some episodes left unlabelled), `typology`, `scenario_id`, `benign_scenario` | Offline training/evaluation only; never ingested |
| `customers.parquet` | `segment`, `home_city`, benign flags | Analysis only; never ingested, never a model input |
| `splits.parquet` | `train` / `validation` / `test` / `holdout` / `excluded` per transaction | Training and evaluation |
