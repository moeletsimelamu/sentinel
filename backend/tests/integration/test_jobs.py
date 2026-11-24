"""Job queue: claiming under concurrency, retry with backoff, dead-lettering, recovery."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from sentinel.core.config import get_settings
from sentinel.core.permissions import Role
from sentinel.modules.audit.models import AuditLog
from sentinel.modules.ingestion import service as ingestion
from sentinel.modules.ingestion.models import ImportBatch, ImportFile
from sentinel.modules.jobs import service as jobs
from sentinel.modules.jobs.models import JobStatus, ProcessingJob
from sentinel.worker import JobHandler, process_next
from tests.conftest import AuthHeaders

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "transactions_malformed.csv"
PAST = datetime(2020, 1, 1, tzinfo=UTC)


def _boom(_db: Session, _payload: dict[str, Any]) -> None:
    raise RuntimeError("kaput")


def _ok(_db: Session, _payload: dict[str, Any]) -> None:
    return None


def _run(db: Session, handlers: dict[str, JobHandler]) -> bool:
    return process_next(db, handlers, settings=get_settings(), worker="test-worker")


def test_successful_job(db: Session) -> None:
    job = jobs.enqueue(db, "noop", {"x": 1}, settings=get_settings())
    db.commit()
    assert _run(db, {"noop": JobHandler(_ok)})
    db.refresh(job)
    assert job.status == JobStatus.SUCCEEDED and job.attempts == 1
    assert job.finished_at is not None and job.locked_by is None
    assert not _run(db, {"noop": JobHandler(_ok)})  # queue now empty


def test_failed_job_retries_with_backoff_then_dies(db: Session) -> None:
    dead_calls: list[tuple[dict[str, Any], str]] = []

    def _on_dead(_db: Session, payload: dict[str, Any], error: str) -> None:
        dead_calls.append((payload, error))

    handlers = {"boom": JobHandler(_boom, _on_dead)}
    job = jobs.enqueue(db, "boom", {"n": 1}, settings=get_settings())
    db.commit()
    settings = get_settings()

    before = datetime.now(UTC)
    assert _run(db, handlers)
    db.refresh(job)
    assert job.status == JobStatus.QUEUED and job.attempts == 1
    assert job.last_error == "RuntimeError: kaput"
    assert job.run_after >= before + timedelta(seconds=settings.job_retry_base_seconds)
    assert not _run(db, handlers), "a backed-off job must not be claimed early"

    for attempt in range(2, settings.job_max_attempts + 1):
        job.run_after = PAST
        db.commit()
        assert _run(db, handlers)
        db.refresh(job)
        assert job.attempts == attempt

    assert job.status == JobStatus.DEAD and job.finished_at is not None
    assert dead_calls == [({"n": 1}, "RuntimeError: kaput")]


def test_backoff_grows_and_is_capped() -> None:
    settings = get_settings()
    delays = [jobs.backoff(settings, n) for n in range(1, 20)]
    assert delays[0] == timedelta(seconds=settings.job_retry_base_seconds)
    assert delays[1] == 2 * delays[0]
    assert max(delays) == jobs.MAX_BACKOFF


def test_unknown_job_kind_goes_straight_to_dead(db: Session) -> None:
    job = jobs.enqueue(db, "mystery", {}, settings=get_settings())
    db.commit()
    assert _run(db, {})
    db.refresh(job)
    assert job.status == JobStatus.DEAD
    assert job.last_error is not None and "mystery" in job.last_error


def test_stale_running_job_is_requeued(db: Session) -> None:
    job = jobs.enqueue(db, "noop", {}, settings=get_settings())
    job.status = JobStatus.RUNNING
    job.attempts = 1
    job.locked_by = "crashed-worker"
    job.locked_at = datetime.now(UTC) - timedelta(hours=2)
    db.commit()
    assert jobs.requeue_stale(db, settings=get_settings()) == 1
    db.refresh(job)
    assert job.status == JobStatus.QUEUED and job.locked_by is None
    assert job.last_error == "Worker lock expired"


def test_database_errors_are_described_without_values() -> None:
    orig = Exception("Key (external_id)=(TX-SECRET) already exists")
    exc = OperationalError("INSERT ...", {"x": "TX-SECRET"}, orig)
    assert "TX-SECRET" not in jobs.describe_error(exc)


def test_dead_ingest_job_marks_batch_failed(
    client: TestClient, auth_headers: AuthHeaders, db: Session
) -> None:
    resp = client.post(
        "/api/v1/imports",
        content=FIXTURE.read_bytes(),
        headers={**auth_headers(Role.ANALYST), "Content-Type": "text/csv"},
    )
    batch_id = uuid.UUID(resp.json()["id"])
    batch = db.get(ImportBatch, batch_id)
    assert batch is not None and batch.job_id is not None
    job = db.get(ProcessingJob, batch.job_id)
    assert job is not None
    job.max_attempts = 1
    db.commit()

    handlers = {
        ingestion.INGEST_CSV_JOB: JobHandler(_boom, on_dead=ingestion.fail_csv_batch),
    }
    assert _run(db, handlers)
    db.refresh(batch)
    assert batch.status == "failed"
    assert batch.file_error is not None and batch.file_error["code"] == "processing_failed"
    assert db.get(ImportFile, batch_id) is None
    actions = [
        a.action for a in db.query(AuditLog).filter(AuditLog.entity_id == str(batch_id)).all()
    ]
    assert actions == ["import.uploaded", "import.failed"]


# ------------------------------------------------------ real concurrency


@pytest.fixture
def committed_jobs(engine: Engine) -> Iterator[list[uuid.UUID]]:
    """Two jobs committed for real, so separate connections can see them."""
    kind = f"concurrency-{uuid.uuid4().hex[:8]}"
    with Session(engine) as s:
        ids = [jobs.enqueue(s, kind, {"i": i}, settings=get_settings()).id for i in range(2)]
        s.commit()
    yield ids
    with Session(engine) as s:
        s.execute(delete(ProcessingJob).where(ProcessingJob.id.in_(ids)))
        s.commit()


def test_concurrent_workers_never_claim_the_same_job(
    engine: Engine, committed_jobs: list[uuid.UUID]
) -> None:
    now = datetime.now(UTC)
    with Session(engine) as worker_a, Session(engine) as worker_b:
        # Worker A has selected and locked a job but not yet committed its claim.
        locked = worker_a.scalars(jobs.claimable(now)).first()
        assert locked is not None and locked.id in committed_jobs

        # Worker B, polling at the same moment, skips the locked row.
        claimed_b = jobs.claim_next(worker_b, "worker-b", now)
        assert claimed_b is not None
        assert claimed_b.id != locked.id and claimed_b.id in committed_jobs
        assert jobs.claim_next(worker_b, "worker-b", now) is None  # A's row stays locked

        worker_a.rollback()
        claimed_after = jobs.claim_next(worker_b, "worker-b", now)
        assert claimed_after is not None and claimed_after.id == locked.id
