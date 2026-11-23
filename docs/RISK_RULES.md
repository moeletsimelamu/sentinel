# Sentinel — Detection Rule Catalogue

> Phase 1 design; implemented in Phase 4. Default thresholds are starting points chosen for the synthetic data and are tuned (and the tuning documented) using the validation split only.

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

## Known weaknesses (by design, to be measured)
- Relative rules are blind for customers with little history; absolute rules then dominate.
- `low_and_slow` fraud is expected to trigger almost nothing.
- `NEW_LOCATION_HIGH_VALUE` and `NEW_DEVICE_HIGH_VALUE` will fire on legitimate travel and new phones — this is the false-positive load ML is meant to reduce.

Rule effectiveness (hit rate, precision against analyst outcomes, share of alerts per rule) is reported in the monitoring dashboard.
