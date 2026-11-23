# Sentinel — Implementation Roadmap

Each phase ends with: a summary of completed functionality, key decisions, files changed, test results (failures reported honestly), outstanding issues — and a stop for review before the next phase. Each completed phase is committed (conventional commits, sole author) and pushed after diff review, tests, secret scan and attribution check.

| Phase | Deliverables | Exit criteria (tests that must pass) |
|---|---|---|
| **1. Requirements & architecture** | REQUIREMENTS, ARCHITECTURE, DATABASE, DATA_PIPELINE, MODEL_EVALUATION (method), RISK_RULES, this roadmap, README skeleton | Design reviewed and approved |
| **2. Backend foundation** | Repo scaffold, Docker Compose (Postgres, API, worker), settings, structured logging + redaction, error envelope, Alembic baseline (users, roles, permissions, refresh tokens, audit_log + append-only trigger), auth (login/refresh/logout), RBAC dependency, seed admin/analyst/auditor users, health endpoint, CI skeleton (lint, type-check, pytest) | Auth flow integration tests; route×role permission matrix test; audit trigger test; `docker compose up` from clean clone |
| **3. Ingestion & validation** | Customers/transactions/import tables, CSV upload (streamed, size-limited), REST ingestion, validation rules, duplicate detection, rejected-record report, import history, job queue + worker | Validation unit tests per rule; CSV import integration test with malformed fixture; idempotent re-import test; worker claim/retry test |
| **4. Rule-based detection** | `detection/` pure engine, rule catalogue v1, versioned rule config API (admin), rule sets, rules-only scoring path, rule_hits storage | Unit test per rule (hit/miss/boundary); config versioning + audit tests; end-to-end import → rule hits |
| **5. Synthetic data & features** | Seeded generator (profiles, typologies, benign anomalies, label noise), dataset cards, `features/` pure pipeline, leakage-safe windows, train/val/test + customer-disjoint split, EDA/outlier notebook | Generator determinism test; feature unit tests; **no-future-leakage test**; train/serve parity test; schema test excluding label columns |
| **6. ML experimentation** | Pipelines for LR, RF, IF; rule baseline evaluation; calibration; threshold selection; metrics, curves, per-typology & error analysis; model manifests; registry integration; MODEL_EVALUATION results; MODEL_CARD | Metric function tests on hand-computed cases; manifest/hash verification tests; reproducible run from a single command |
| **7. Risk scoring & alerts** | Inference loader, explanations, scoring config versions, assessments, alert policy + deduplication, alert API (queue, assign, transitions, notes, resolution), optimistic locking | State-machine tests incl. illegal transitions; 409 on stale version; explanation content tests; permissions |
| **8. Investigations** | Case API: create from alerts, link transactions, notes, evidence refs, assign, escalate (four-eyes), close with outcome, timeline; customer activity view | Lifecycle integration tests; four-eyes test; append-only notes test; traceability (txn ↔ alert ↔ case) test |
| **9. Frontend & monitoring** | Design tokens + app shell, login, dashboard, transaction explorer/detail, alert queue, investigation workspace, model performance & drift pages, data management, admin config, audit viewer; drift job (PSI) and monitoring API | Vitest component tests; drift computation tests; Playwright smoke for each role |
| **10. Hardening & docs** | Rate limits verified, security review, dependency audit, CI with Postgres service + Playwright, coverage report, all docs finalised (SECURITY, TESTING, DEPLOYMENT, DATA_DICTIONARY, MODEL_CARD), clean-setup walkthrough | Full suite green in CI; reviewer walkthrough of all 14 Definition-of-Done items from a clean clone |

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Models look unrealistically good on synthetic data | Benign anomalies, hard typologies, label noise, single-feature leakage check, explicit disclaimers |
| Train/serve feature skew | One shared pure feature module + parity test |
| Scope creep in the UI | UI built only after APIs are stable (Phase 9); pages map 1:1 to Definition of Done |
| Local Python 3.14 lacks scientific wheels | Docker images pin Python 3.12; local venv instructions specify 3.12 |
| Feature computation slow on large batches | Customer-grouped vectorised pandas; history limited to 90-day lookback; index on `(customer_id, occurred_at)` |
