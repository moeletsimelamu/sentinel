"""Background worker entry point: ``python -m sentinel.worker``.

Phase 2 provides the process skeleton (graceful shutdown, DB heartbeat). Phase 3 adds the
``processing_jobs`` queue, claimed with ``SELECT ... FOR UPDATE SKIP LOCKED``.
"""

from __future__ import annotations

import signal
import threading
from types import FrameType

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from sentinel.core.config import get_settings
from sentinel.core.db import get_engine
from sentinel.core.logging import configure_logging, get_logger

log = get_logger("sentinel.worker")

POLL_INTERVAL_SECONDS = 5.0


def run(stop: threading.Event) -> None:
    log.info("worker_started")
    while not stop.is_set():
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError as exc:
            log.warning("worker_db_unavailable", error_type=type(exc).__name__)
        # Phase 3: claim and process jobs here.
        stop.wait(POLL_INTERVAL_SECONDS)
    log.info("worker_stopped")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    stop = threading.Event()

    def _handle(signum: int, _frame: FrameType | None) -> None:
        log.info("worker_shutdown_requested", signal=signum)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    run(stop)


if __name__ == "__main__":
    main()
