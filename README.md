# Sentinel

**Financial fraud detection and risk analytics — an educational simulation.**

Sentinel ingests synthetic financial transactions, scores them with configurable rules and machine-learning models, explains why each transaction was flagged, and supports analysts through alert triage and investigation case management with a complete audit trail.

> ⚠️ Sentinel uses **synthetic data only**. It does not connect to banking infrastructure, does not process real personal or card data, and is not a production fraud-prevention system. Model results describe performance on generated data and do not indicate real-world detection performance. A risk score is an investigative indicator, not proof of fraud.

## Status

| Phase | State |
|---|---|
| 1 — Requirements, domain model, architecture | ✅ Complete |
| 2 — Backend foundation, database, authentication | ✅ Complete |
| 3–10 | Not started |

## Design documents

- [Requirements & domain model](docs/REQUIREMENTS.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Database design](docs/DATABASE.md)
- [Synthetic data & feature pipeline](docs/DATA_PIPELINE.md)
- [Model evaluation methodology](docs/MODEL_EVALUATION.md)
- [Detection rule catalogue](docs/RISK_RULES.md)
- [Implementation roadmap](docs/ROADMAP.md)
- [Security](docs/SECURITY.md)

## Planned stack

FastAPI · SQLAlchemy 2 · Pydantic 2 · Alembic · PostgreSQL · pandas · NumPy · scikit-learn · React · TypeScript · Vite · TanStack Query · Docker Compose · GitHub Actions · pytest · Vitest · Playwright

## Quick start (Phase 2 backend)

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

### Backend tests

```powershell
cd backend; python -m uv sync --python 3.12; .\.venv\Scripts\python.exe -m pytest
```

Tests use the `sentinel_test` database in the compose Postgres and roll back after each test.
