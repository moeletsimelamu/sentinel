"""Background worker entry point: ``python -m sentinel.worker``.

Polls ``processing_jobs`` and runs each claimed job in its own database transaction.
Several workers can run at once: claims use ``FOR UPDATE SKIP LOCKED``.
"""

from __future__ import annotations

import os
import signal
import socket
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import FrameType
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from sentinel.core.config import Settings, get_settings
from sentinel.core.db import get_session_factory
from sentinel.core.logging import configure_logging, get_logger
from sentinel.modules.auth import models as _auth_models  # noqa: F401 - FK targets
from sentinel.modules.ingestion import service as ingestion
from sentinel.modules.jobs import service as jobs
from sentinel.modules.jobs.models import JobStatus

log = get_logger("sentinel.worker")

MAX_JOBS_PER_TICK = 50
STALE_CHECK_EVERY_TICKS = 30


@dataclass(frozen=True)
class JobHandler:
    handle: Callable[[Session, dict[str, Any]], None]
    # Called in a fresh transaction once the job has failed for the last time.
    on_dead: Callable[[Session, dict[str, Any], str], None] | None = None


def default_handlers(settings: Settings) -> dict[str, JobHandler]:
    return {
        ingestion.INGEST_CSV_JOB: JobHandler(
            handle=lambda db, payload: ingestion.process_csv_batch(db, settings, payload),
            on_dead=ingestion.fail_csv_batch,
        ),
    }


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def process_next(
    db: Session,
    handlers: Mapping[str, JobHandler],
    *,
    settings: Settings,
    worker: str,
) -> bool:
    """Claim and run one job. Returns False when the queue had nothing runnable."""
    job = jobs.claim_next(db, worker)
    if job is None:
        return False
    job_id, kind, payload = job.id, job.kind, dict(job.payload)
    bound = log.bind(job_id=str(job_id), kind=kind, attempt=job.attempts)
    handler = handlers.get(kind)
    if handler is None:
        bound.error("job_unknown_kind")
        job.attempts = job.max_attempts
        jobs.mark_failed(db, job, f"No handler for job kind '{kind}'", settings=settings)
        db.commit()
        return True
    try:
        handler.handle(db, payload)
        claimed = jobs.get_job(db, job_id)
        if claimed is not None and claimed.status == JobStatus.RUNNING:
            jobs.mark_succeeded(db, claimed)
        db.commit()
        bound.info("job_succeeded")
    except Exception as exc:
        db.rollback()
        error = jobs.describe_error(exc)
        claimed = jobs.get_job(db, job_id)
        if claimed is None:  # pragma: no cover - the claim was committed
            return True
        dead = jobs.mark_failed(db, claimed, error, settings=settings)
        db.commit()
        if dead:
            bound.error("job_dead", error=error)
            if handler.on_dead is not None:
                try:
                    handler.on_dead(db, payload, error)
                    db.commit()
                except Exception as hook_exc:
                    db.rollback()
                    bound.error("job_on_dead_failed", error=jobs.describe_error(hook_exc))
        else:
            bound.warning("job_failed_will_retry", error=error)
    return True


def run(
    stop: threading.Event,
    *,
    settings: Settings,
    handlers: Mapping[str, JobHandler],
    session_factory: Callable[[], Session],
) -> None:
    me = worker_id()
    log.info("worker_started", worker=me, kinds=sorted(handlers))
    tick = 0
    while not stop.is_set():
        try:
            with session_factory() as db:
                if tick % STALE_CHECK_EVERY_TICKS == 0:
                    recovered = jobs.requeue_stale(db, settings=settings)
                    if recovered:
                        log.warning("jobs_recovered_from_stale_lock", count=recovered)
                for _ in range(MAX_JOBS_PER_TICK):
                    if stop.is_set() or not process_next(
                        db, handlers, settings=settings, worker=me
                    ):
                        break
        except SQLAlchemyError as exc:
            log.warning("worker_db_unavailable", error=jobs.describe_error(exc))
        tick += 1
        stop.wait(settings.worker_poll_seconds)
    log.info("worker_stopped", worker=me)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    stop = threading.Event()

    def _handle(signum: int, _frame: FrameType | None) -> None:
        log.info("worker_shutdown_requested", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    run(
        stop,
        settings=settings,
        handlers=default_handlers(settings),
        session_factory=get_session_factory(),
    )


if __name__ == "__main__":
    main()
