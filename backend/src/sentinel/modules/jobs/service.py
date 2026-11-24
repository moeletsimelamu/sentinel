"""Postgres-backed job queue.

Lifecycle: ``queued`` → (claim) ``running`` → ``succeeded``; on error the job returns to
``queued`` with an exponential backoff until ``max_attempts`` is reached, then ``dead``.

Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so several workers can poll the same
table without ever receiving the same job. A job whose worker died mid-run is recovered
by ``requeue_stale`` after the lock timeout.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from sentinel.core.config import Settings
from sentinel.modules.jobs.models import JobStatus, ProcessingJob

MAX_BACKOFF = timedelta(minutes=15)
MAX_ERROR_LENGTH = 500


def _now() -> datetime:
    return datetime.now(UTC)


def enqueue(
    db: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    settings: Settings,
    run_after: datetime | None = None,
) -> ProcessingJob:
    """Add a job to the caller's transaction — it becomes visible when the caller commits."""
    job = ProcessingJob(
        kind=kind,
        payload=payload,
        status=JobStatus.QUEUED,
        attempts=0,
        max_attempts=settings.job_max_attempts,
        run_after=run_after or _now(),
    )
    db.add(job)
    db.flush()
    return job


def claimable(now: datetime) -> Select[ProcessingJob]:
    """The oldest runnable job, skipping rows another worker has locked."""
    return (
        select(ProcessingJob)
        .where(ProcessingJob.status == JobStatus.QUEUED, ProcessingJob.run_after <= now)
        .order_by(ProcessingJob.run_after, ProcessingJob.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def claim_next(db: Session, worker_id: str, now: datetime | None = None) -> ProcessingJob | None:
    """Atomically claim the oldest runnable job and commit the claim."""
    now = now or _now()
    job = db.scalars(claimable(now)).first()
    if job is None:
        db.rollback()  # release the snapshot; nothing was locked
        return None
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.locked_by = worker_id
    job.locked_at = now
    db.commit()
    return job


def mark_succeeded(db: Session, job: ProcessingJob, now: datetime | None = None) -> None:
    job.status = JobStatus.SUCCEEDED
    job.finished_at = now or _now()
    job.locked_by = None
    job.locked_at = None
    job.last_error = None


def describe_error(exc: BaseException) -> str:
    """A short, log-safe description. Database errors can echo row values, so only the
    exception type and SQLSTATE are kept for them."""
    if isinstance(exc, SQLAlchemyError):
        orig = getattr(exc, "orig", None)
        sqlstate = getattr(orig, "sqlstate", None)
        suffix = f" (sqlstate {sqlstate})" if sqlstate else ""
        return f"{type(exc).__name__}{suffix}"
    message = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {message}"[:MAX_ERROR_LENGTH]


def backoff(settings: Settings, attempts: int) -> timedelta:
    delay = timedelta(seconds=settings.job_retry_base_seconds * 2 ** max(attempts - 1, 0))
    return min(delay, MAX_BACKOFF)


def mark_failed(
    db: Session,
    job: ProcessingJob,
    error: str,
    *,
    settings: Settings,
    now: datetime | None = None,
) -> bool:
    """Record a failed attempt. Returns True when the job is now dead (no more retries)."""
    now = now or _now()
    job.last_error = error[:MAX_ERROR_LENGTH]
    job.locked_by = None
    job.locked_at = None
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.DEAD
        job.finished_at = now
        return True
    job.status = JobStatus.QUEUED
    job.run_after = now + backoff(settings, job.attempts)
    return False


def requeue_stale(db: Session, *, settings: Settings, now: datetime | None = None) -> int:
    """Return jobs abandoned by a crashed worker to the queue (or mark them dead)."""
    now = now or _now()
    cutoff = now - timedelta(minutes=settings.job_lock_timeout_minutes)
    stale = db.scalars(
        select(ProcessingJob)
        .where(ProcessingJob.status == JobStatus.RUNNING, ProcessingJob.locked_at < cutoff)
        .with_for_update(skip_locked=True)
    ).all()
    for job in stale:
        mark_failed(db, job, "Worker lock expired", settings=settings, now=now)
    db.commit()
    return len(stale)


def get_job(db: Session, job_id: uuid.UUID) -> ProcessingJob | None:
    return db.get(ProcessingJob, job_id)
