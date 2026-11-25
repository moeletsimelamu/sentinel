# Sentinel — Detection Rule Catalogue

> Phase 1 design; **implemented in Phase 4** (`backend/src/sentinel/detection/`). Default thresholds are starting points chosen for the synthetic data and are tuned (and the tuning documented) using the validation split only.

## Rule model

Each rule is a pure function over a transaction's feature row:

```python
class Rule(Protocol):
    code: str
    params_schema: type[BaseModel]     # validates admin-supplied parameters
    def evaluate(self, row: FeatureRow, params: BaseModel) -> RuleHit | None: ...
```

A `RuleHit` records the observed value(s), the threshold(s) and a rendered explanation string. Administrators can change parameters, weight, severity, `force_alert` and active status — each change creates a new `rule_version` with a mandatory change reason, and is audited. Rules cannot be authored as free-form code through the UI (that would be a code-injection surface); new rule *types* are added in code and reviewed.

## Catalogue (v1)

| Code | Pattern (requirement) | Logic (default parameters) | Weight | Severity |
|---|---|---|---|---|
| `LARGE_AMOUNT_ABSOLUTE` | Unusually large transaction | amount ≥ 50,000 (currency-normalised) | 30 | High |
| `LARGE_AMOUNT_RELATIVE` | Spending behaviour change | amount ≥ 8 × customer 90d median **and** history_count_90d ≥ 10 | 25 | Medium |
| `RAPID_REPEAT` | Rapid repeated transactions | ≥ 3 transactions to the same merchant category within 10 min | 20 | Medium |
| `VELOCITY_1H` | Unusual frequency | txn_count_1h ≥ 6 | 20 | Medium |
| `VELOCITY_RATIO_24H` | Unusual frequency vs. own baseline | velocity_ratio_24h_vs_30d ≥ 5 **and** txn_count_24h ≥ 5 | 15 | Medium |
| `FAILED_ATTEMPTS` | Multiple failed transactions | failed_count_24h ≥ 3 | 25 | High |
| `IMPOSSIBLE_TRAVEL` | Unusual geographic change | implied_speed_kmh ≥ 900 **and** km_from_prev_txn ≥ 300 | 35 | High (`force_alert`) |
| `NEW_LOCATION_HIGH_VALUE` | Unusual geography | is_new_city_90d **and** amount ≥ 3 × customer median | 15 | Low |
| `UNUSUAL_HOUR` | Unusual timing | hour_rarity_cust ≥ 0.97 **and** amount ≥ 2 × customer median | 10 | Low |
| `NEW_DEVICE_HIGH_VALUE` | Outside behavioural pattern | is_new_device_90d **and** amount ≥ 3 × customer median | 20 | Medium |
| `HIGH_RISK_CATEGORY_SHIFT` | Sudden change in behaviour | high_risk_category **and** is_new_category_90d | 20 | Medium |
| `BEHAVIOUR_SHIFT` | Outside established pattern | category_share_shift_7d_vs_90d ≥ 0.5 **and** history_count_90d ≥ 20 | 10 | Low |

Score contribution: `rule_component = min(rule_cap, Σ weights)` with `rule_cap = 100` by default (see ARCHITECTURE.md §5).

## Implementation notes (Phase 4)

- **Features.** Rules read a `FeatureRow` from `sentinel.features.build_feature_rows` — pure Python, sliding windows per customer, using only that customer's transactions **strictly before** the one being scored (same-instant transactions do not see each other). A property test asserts that adding later transactions never changes earlier features. Missing history gives `None` ("unknown"), never 0, and every relative rule is silent on `None` or when `history_count_90d` is below its `min_history`.
- **Counts.** Velocity features count *prior* transactions: `VELOCITY_1H` fires when ≥ 6 transactions happened in the hour before; `FAILED_ATTEMPTS` when ≥ 3 declined/failed attempts happened in the previous 24 h (so the first success after a burst of declines is flagged). `RAPID_REPEAT` counts *including* the current transaction (≥ 3 in the same category within 10 min).
- **Currency.** Amount comparisons use ZAR via fixed illustrative rates (`core/vocabulary.FX_TO_ZAR`: USD 18, EUR 19.5, GBP 23). The customer median uses completed transactions only.
- **Extra guards** added during implementation: `min_history` on `NEW_LOCATION_HIGH_VALUE` (5), `UNUSUAL_HOUR` (10), `NEW_DEVICE_HIGH_VALUE` (5) and `HIGH_RISK_CATEGORY_SHIFT` (5), so first-time customers do not trip "new city/device/category" rules on their first transactions.
- **Parameters** are typed and bounded per rule (Pydantic, extra keys refused). `GET /api/v1/rules/{code}` returns the JSON schema.
- **Versioning.** `POST /api/v1/rules/{code}/versions` (admin, `rules:configure`) takes `expected_version` (409 if stale), a `change_reason` (≥ 10 characters) and any of `params` (merged over current), `weight`, `severity`, `force_alert`, `is_active`. It writes a new `rule_versions` row and a new `rule_sets` snapshot in one transaction, audited as `rule.version_created`. All four rule tables are append-only.
- **Scoring (rules-only, Phase 4).** `rule_score = min(rule_cap, Σ weights)`. With no model the risk score *is* the rule score (otherwise a missing model would cap every score at `w_rule × 100`). Phase 7 adds the model component and the alert policy; `force_alert` is already recorded on each assessment.
- **When scoring runs.** Every completed import with accepted rows queues a `score_batch` job. `POST /api/v1/detection/runs` (`detection:run`) re-scores a batch with the current rule set; earlier assessments are kept, so history shows how a configuration change altered the outcome.
- **Explanations** are rendered by each rule from observed values and thresholds, e.g. *"Amount ZAR 60,000.00 is 600.0x this customer's 90-day median (threshold 8.0x)"*. They describe unusual activity and never use the word "fraud" (tested).

## Known weaknesses (by design, to be measured)
- Relative rules are blind for customers with little history; absolute rules then dominate.
- `low_and_slow` fraud is expected to trigger almost nothing.
- Hour-of-day rarity uses UTC. Because it compares a customer with their own history it is unaffected by a fixed timezone offset, but it would be confused by a customer who changes timezone.
- `NEW_LOCATION_HIGH_VALUE` and `NEW_DEVICE_HIGH_VALUE` will fire on legitimate travel and new phones — this is the false-positive load ML is meant to reduce.
- **Observed on the demo dataset (Phase 5, all 97,151 transactions, default v1 thresholds):** `BEHAVIOUR_SHIFT` fired on 27.5% of transactions and `LARGE_AMOUNT_RELATIVE` on 6.4%. A 7-day category mix built from a handful of transactions is almost always far from the 90-day mix, so the 0.5 threshold is too loose. These rules will be recalibrated in Phase 6 **on the validation split only** (tuning on all data would leak test information), and the change recorded as a new rule version.

Rule effectiveness (hit rate, precision against analyst outcomes, share of alerts per rule) is reported in the monitoring dashboard.
