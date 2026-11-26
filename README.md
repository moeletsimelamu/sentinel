# Sentinel

**Financial fraud detection and risk analytics — an educational simulation.**

Sentinel ingests synthetic financial transactions, scores them with configurable rules and machine-learning models, explains why each transaction was flagged, and supports analysts through alert triage and investigation case management with a complete audit trail.

> ⚠️ Sentinel uses **synthetic data only**. It does not connect to banking infrastructure, does not process real personal or card data, and is not a production fraud-prevention system. Model results describe performance on generated data and do not indicate real-world detection performance. A risk score is an investigative indicator, not proof of fraud.

## Status

| Phase | State |
|---|---|
| 1 — Requirements, domain model, architecture | ✅ Complete |
| 2 — Backend foundation, database, authentication | ✅ Complete |
| 3 — Ingestion & validation | ✅ Complete |
| 4 — Rule-based detection | ✅ Complete |
| 5 — Synthetic data & feature engineering | ✅ Complete |
| 6 — ML experimentation & evaluation | ✅ Complete |
| 7 — Risk scoring & alerts | ✅ Complete |
| 8–10 | Not started |

## Design documents

- [Requirements & domain model](docs/REQUIREMENTS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Database design](docs/DATABASE.md)
- [Synthetic data & feature pipeline](docs/DATA_PIPELINE.md)
- [Data dictionary & import format](docs/DATA_DICTIONARY.md)
- [Model evaluation methodology & results](docs/MODEL_EVALUATION.md)
- [Model card](docs/MODEL_CARD.md)
- [Detection rule catalogue & implementation notes](docs/RISK_RULES.md)
- [Implementation roadmap](docs/ROADMAP.md)
- [Security](docs/SECURITY.md)

## Planned stack

FastAPI · SQLAlchemy 2 · Pydantic 2 · Alembic · PostgreSQL · pandas · NumPy · scikit-learn · React · TypeScript · Vite · TanStack Query · Docker Compose · GitHub Actions · pytest · Vitest · Playwright

## Quick start (backend)

Requires Docker Desktop. Optional: copy `.env.example` to `.env` to override defaults.

```powershell
docker compose up -d --build
```

```powershell
docker compose exec -e SENTINEL_DEMO_PASSWORD=Sentinel-Demo-2026! api python -m sentinel.cli seed-demo-users
```

- API docs: http://localhost:8088/api/docs · readiness: http://localhost:8088/api/v1/health/ready
- Postgres is exposed on `127.0.0.1:5438` (ports chosen to avoid the other portfolio stacks).
- Demo accounts: `admin@`, `analyst@`, `lead.analyst@`, `auditor@sentinel.local` (dev-only password from `.env.example`).

### Import transactions

A small sample with deliberately malformed rows is in `backend/tests/fixtures/transactions_malformed.csv`; a full synthetic dataset can be generated (see below). Upload a file as an analyst, then read the summary and the rejected-record report:

```powershell
$token = (Invoke-RestMethod -Method Post -Uri http://localhost:8088/api/v1/auth/login -ContentType application/json -Body '{"email":"analyst@sentinel.local","password":"Sentinel-Demo-2026!"}').access_token
```

```powershell
$batch = Invoke-RestMethod -Method Post -Uri "http://localhost:8088/api/v1/imports?filename=sample.csv" -ContentType text/csv -InFile backend/tests/fixtures/transactions_malformed.csv -Headers @{ Authorization = "Bearer $token" }
```

```powershell
Invoke-RestMethod -Uri "http://localhost:8088/api/v1/imports/$($batch.id)" -Headers @{ Authorization = "Bearer $token" }
```

```powershell
(Invoke-RestMethod -Uri "http://localhost:8088/api/v1/imports/$($batch.id)/issues" -Headers @{ Authorization = "Bearer $token" }).items | Format-Table row_number, outcome, external_id
```

The worker processes the upload within a few seconds (`queued` → `completed`). Uploading the same file again stores nothing new: every row is reported as a duplicate. Small batches can also be sent as JSON to `POST /api/v1/transactions`. Field rules and every validation code are listed in the [data dictionary](docs/DATA_DICTIONARY.md).

### Rule-based detection

Each completed import is scored automatically by the worker (rules-only until a model is trained in Phase 6). Look at why a transaction was flagged:

```powershell
$txn = (Invoke-RestMethod -Uri "http://localhost:8088/api/v1/transactions?batch_id=$($batch.id)" -Headers @{ Authorization = "Bearer $token" }).items[0]
```

```powershell
Invoke-RestMethod -Uri "http://localhost:8088/api/v1/transactions/$($txn.id)/assessments" -Headers @{ Authorization = "Bearer $token" } | ConvertTo-Json -Depth 6
```

Admins tune rules with `POST /api/v1/rules/{code}/versions` (new version + new rule set, audited); analysts re-score an import with `POST /api/v1/detection/runs`. See [RISK_RULES.md](docs/RISK_RULES.md).

### Synthetic data

```powershell
cd ml; python -m uv sync --python 3.12; .\.venv\Scripts\sentinel-ml.exe generate --profile demo --seed 42 --out ..\data
```

Writes `data/demo-1.0.0-42/` (about 97k transactions, git-ignored) with a dataset card. Upload `ingest/transactions_part_001.csv` with the import command above to load it into the app; the labels stay in `labels.parquet` and are never ingested. The exploration notebook is `ml/notebooks/01_eda_outliers.ipynb`.

### Train and evaluate models

```powershell
cd ml; .\.venv\Scripts\sentinel-ml.exe experiment --profile full --seed 42
```

Generates `full-1.0.0-42` if needed (about 380k transactions), then runs the whole protocol in [MODEL_EVALUATION.md](docs/MODEL_EVALUATION.md): rule baseline, logistic regression, random forest, isolation forest and a hybrid, tuned and thresholded on validation, evaluated once on test with bootstrap intervals. About 20 minutes on a laptop. Writes artefacts with manifests to `models/` (git-ignored) and the report to `docs/reports/`. Register them with the running app (hashes and feature schema are verified; artefacts are never unpickled at registration):

```powershell
docker compose exec api python -m sentinel.cli register-models
```

Compare them at `GET /api/v1/models`. Results and the selected model are described in the [model card](docs/MODEL_CARD.md).

### Alerts and model-based scoring

```powershell
$admin = (Invoke-RestMethod -Method Post -Uri http://localhost:8088/api/v1/auth/login -ContentType application/json -Body '{"email":"admin@sentinel.local","password":"Sentinel-Demo-2026!"}').access_token
```

Activate the model selected in Phase 6 (its hash and feature schema are re-verified before the switch):

```powershell
$rf = (Invoke-RestMethod -Uri http://localhost:8088/api/v1/models -Headers @{ Authorization = "Bearer $admin" }) | Where-Object selected_by_experiment
```

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8088/api/v1/models/$($rf.id)/activate" -ContentType application/json -Body '{"reason":"Selected on validation in Phase 6"}' -Headers @{ Authorization = "Bearer $admin" }
```

From then on every scored transaction gets a combined rules + model score with explanations, and alerts appear in `GET /api/v1/alerts` (open alerts, highest priority first). Analysts work them with `POST /api/v1/alerts/{id}/claim`, `/start`, `/close` (resolution reason + findings) and `/notes`; lead analysts `/assign` and `/reopen`. Each action sends the alert's current `version`; a stale one returns 409.

### Backend tests

```powershell
cd backend; python -m uv sync --python 3.12; .\.venv\Scripts\python.exe -m pytest
```

Tests use the `sentinel_test` database in the compose Postgres and roll back after each test.
