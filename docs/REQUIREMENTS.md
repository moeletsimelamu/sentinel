# Sentinel — Requirements & Domain Model

> **Status:** Phase 1 design. Sentinel is an educational simulation built on synthetic data. It does not connect to banking infrastructure, does not process real customer data, and is not a production fraud-prevention system.

## 1. Problem statement

Fraud operations teams receive a stream of transactions, most of them legitimate. They need to (a) surface the small fraction that look unusual, (b) explain *why* each one was surfaced, (c) route that work to analysts, and (d) record what humans concluded — with a complete, tamper-evident history. Sentinel simulates that workflow end to end and shows how rule-based detection and machine learning can be combined, evaluated honestly, and monitored over time.

## 2. Scope

### In scope
- CSV and REST ingestion of synthetic transactions, with validation, duplicate detection and rejected-record reporting.
- Configurable rule-based detection.
- An offline ML experimentation pipeline (rule baseline vs. supervised vs. anomaly detection) and an online inference interface.
- Risk scoring that combines rule hits and model output, with stored explanations.
- Alert management and investigation case management with full activity history.
- Model evaluation reporting, model registry/versioning and basic drift monitoring.
- Role-based access control, audit trail, structured logging.

### Out of scope (explicitly)
- Real payment rails, card data (PAN/CVV), bank credentials, KYC documents.
- Regulatory filing (SAR/STR submission). Sentinel may *mark* a case "referred" but never submits anything.
- Real-time sub-100ms authorisation decisions. Sentinel is a post-transaction monitoring system.
- Microservices, streaming platforms (Kafka), multi-tenancy.

## 3. Users and permissions

Three roles, enforced in the backend via permission checks (the frontend only hides what the API would refuse anyway).

| Permission | Administrator | Fraud Analyst | Auditor |
|---|:-:|:-:|:-:|
| `users:manage` | ✅ | | |
| `rules:read` | ✅ | ✅ | ✅ |
| `rules:configure` (thresholds, activation) | ✅ | | |
| `scoring:configure` (weights, bands) | ✅ | | |
| `models:activate` (promote a model version) | ✅ | | |
| `transactions:read` | ✅ | ✅ | ✅ |
| `ingestion:import` | ✅ | ✅ | |
| `detection:run` (re-score a batch) | ✅ | ✅ | |
| `alerts:read` | ✅ | ✅ | ✅ |
| `alerts:work` (assign self, notes, status) | | ✅ | |
| `alerts:assign_others` | ✅ | ✅ (lead) | |
| `cases:read` | ✅ | ✅ | ✅ |
| `cases:work` (create, link, note, escalate, resolve) | | ✅ | |
| `audit:read` | ✅ | | ✅ |
| `models:read` (evaluations, drift) | ✅ | ✅ | ✅ |
| `reports:read` | ✅ | ✅ | ✅ |

Design notes:
- Administrators deliberately **cannot** resolve alerts or cases — separation of duties between configuring detection and adjudicating its output.
- Auditors are strictly read-only; every auditor endpoint is `GET`.
- Analysts cannot change rules or models, so they cannot tune detection to reduce their own queue.
- "Lead analyst" is modelled as an extra permission grant, not a fourth role.

## 4. Domain glossary

The platform must keep these concepts distinct — in the data model, the API and the UI wording.

| Term | Meaning | What it is **not** |
|---|---|---|
| **Transaction** | A synthetic financial event as ingested. Immutable after acceptance. | — |
| **Unusual activity** | A transaction whose features deviate from the customer's own baseline or population norms. | Not an accusation. |
| **Rule hit** | A specific configured rule evaluated true for a transaction, with the observed value and threshold. | Not a decision. |
| **Model output** | A model's score (0–1) for a transaction, with model version and contributing features. | Not a probability of guilt. |
| **Risk assessment** | The combined, versioned result of scoring one transaction: score 0–100, category, reasons, rule hits, model version, config version, timestamp. Append-only. | Not proof of fraud. |
| **Alert** | A system-generated work item created when an assessment crosses the alert policy. | Not a finding. |
| **Investigation case** | An analyst-owned container linking one or more alerts/transactions, notes, evidence references and an outcome. | — |
| **Investigation outcome** | The human conclusion recorded on a closed case. | The *only* place "confirmed" language may appear. |
| **Synthetic ground truth** | The generator's label for whether a record was injected as a fraud pattern. Used only for offline evaluation. | Never shown in the analyst workflow, never an input to scoring. |

UI copy rule: alerts say *"flagged for review"*, never *"fraudulent"*. A customer is never labelled fraudulent by the system.

## 5. Core workflows

### 5.1 Ingest → score → alert
1. User uploads CSV (or a client POSTs to `/api/v1/transactions`).
2. Rows validated; rejected rows stored with reasons; duplicates detected by `(source_system, external_id)` and content hash.
3. Accepted transactions are queued for scoring as a background job.
4. Worker builds features (using only the customer's history *before* each transaction), evaluates active rules, calls the active model, combines into a risk assessment.
5. If the assessment meets the alert policy, an alert is created (or an existing open alert for the same customer is updated with a linked transaction, to avoid alert storms).

### 5.2 Alert lifecycle

```
NEW ──assign──▶ ASSIGNED ──start──▶ IN_REVIEW ──┬─▶ CLOSED (resolution reason required)
  ▲                                              ├─▶ ESCALATED ──▶ (linked to a case)
  └──────────── reopen (lead only, reason required) ◀───────────────┘
```
Resolution reasons: `false_positive_explained`, `legitimate_customer_behaviour`, `duplicate_alert`, `insufficient_information`, `escalated_to_case`.

As implemented (Phase 7): analysts **claim** a new alert for themselves (`alerts:work`); admins and lead analysts **assign** to any analyst (`alerts:assign_others`, also allowed while assigned or in review). Only the assignee can start review and close; closing needs a reason and written findings. Only a lead analyst (`alerts:work` + `alerts:assign_others`) can reopen a closed alert, which returns it to NEW and unassigned. `escalated_to_case` is not a close reason: ESCALATED is entered by opening a case from the alert (Phase 8). Illegal transitions return 409 `invalid_transition`; acting on a stale `version` returns 409 `stale_version`.

### 5.3 Case lifecycle

```
OPEN ──▶ IN_PROGRESS ──▶ PENDING_REVIEW (escalated to second reviewer) ──▶ CLOSED
                │                                                         ▲
                └──────────────────────────── close ───────────────────────┘
```
Outcomes (required on close): `no_suspicious_activity`, `suspicious_activity_confirmed`, `inconclusive`, `referred_externally_simulated`.
Escalation requires a different user to close the case (four-eyes principle).

### 5.4 No silent overwrites
- Every state change, assignment and note is an append-only **event** row.
- Mutable rows (`alerts`, `cases`) carry a `version` column; updates use optimistic concurrency and return **409 Conflict** if the client's version is stale.
- Notes cannot be edited or deleted; corrections are new notes that reference the original.
- Audit tables are protected by a PostgreSQL trigger that rejects `UPDATE` and `DELETE`.

## 6. Non-functional requirements

| Area | Target (simulation scale) |
|---|---|
| Data volume | ≥ 250k transactions, ≥ 5k customers, comfortably on a laptop |
| CSV import | 50k rows validated and stored in < 60 s; scoring runs asynchronously |
| API p95 (list endpoints, paginated) | < 300 ms on demo data |
| Setup | `docker compose up` + one seed command from a clean clone |
| Security | Argon2 password hashing, short-lived JWT access tokens, refresh rotation, RBAC, rate limiting on auth + ingestion, PII-redacting logs |
| Accessibility | Keyboard-navigable queues, WCAG AA contrast, no colour-only severity cues |

## 7. Definition of Done traceability

| # | Reviewer can… | Delivered in phase |
|---|---|---|
| 1 | Import synthetic transaction data | 3 |
| 2 | Inspect validation results | 3 |
| 3 | Run rule-based detection | 4 |
| 4 | Run experimental ML pipeline | 6 |
| 5 | Compare model evaluation results | 6 (data), 9 (UI) |
| 6 | View flagged transactions | 7 (API), 9 (UI) |
| 7 | Understand why a transaction was flagged | 7 |
| 8 | Create an investigation case | 8 |
| 9 | Assign to an analyst | 8 |
| 10 | Record findings and resolution | 8 |
| 11 | Inspect model and audit history | 7–9 |
| 12 | View reporting dashboards | 9 |
| 13 | Run automated tests | every phase, consolidated in 10 |
| 14 | Follow setup instructions | 2 onward, finalised in 10 |
