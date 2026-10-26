"""Background worker: housekeeping, quota monitoring, notification delivery.

Entry points
------------
  python -m novatrack.worker        (process startup)
  novatrack.worker.main()           (programmatic / test)

Each tick runs four operations in separate transactions so a failure in
one does not abort the others:
  1. Provider operations  (register/stop/delete pending ProviderOperation rows)
  2. Tracking housekeeping (auto-archive, purge old trash)
  3. Quota check           (at most once per 24 h)
  4. Deliver due notifications
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from novatrack.models import AppSettings

__all__ = ["tick", "main"]

logger = logging.getLogger(__name__)

_QUOTA_CHECK_INTERVAL_SECS: int = 86_400  # 24 h


def tick(session_factory, settings, provider) -> None:
    """One worker tick: process_operations → housekeeping → quota → delivery.

    Each step runs in its own transaction; a failure is logged (exception
    class name only — never upstream body, token, or URL) and the next
    step proceeds regardless.
    """
    # 0. Provider operations — runs FIRST so pending register/stop/delete ops
    #    complete before housekeeping or delivery logic depends on their state.
    try:
        with session_factory.begin() as session:
            from novatrack import tracking  # noqa: PLC0415  (lazy, avoids circular import)
            tracking.process_operations(session, provider)
    except Exception as exc:
        logger.error("process_operations failed: %s", type(exc).__name__)

    # 1. Tracking housekeeping (auto-archive, purge)
    try:
        with session_factory.begin() as session:
            from novatrack import tracking  # noqa: PLC0415
            tracking.housekeeping(session, provider)
    except Exception as exc:
        logger.error("Housekeeping failed: %s", type(exc).__name__)

    # 2. Quota check — throttled to once per 24 h
    try:
        with session_factory.begin() as session:
            from novatrack.notifications import check_quota  # noqa: PLC0415
            now = datetime.now(UTC)
            app_settings = session.get(AppSettings, 1)
            due = (
                app_settings is None
                or app_settings.quota_checked_at is None
                or (now - app_settings.quota_checked_at.astimezone(UTC)).total_seconds()
                >= _QUOTA_CHECK_INTERVAL_SECS
            )
            if due:
                check_quota(session, provider, now=now)
    except Exception as exc:
        logger.error("Quota check failed: %s", type(exc).__name__)

    # 3. Deliver due notifications
    try:
        with session_factory.begin() as session:
            from novatrack.notifications import deliver_due  # noqa: PLC0415
            deliver_due(session, settings)
    except Exception as exc:
        logger.error("Delivery failed: %s", type(exc).__name__)


def main() -> None:
    """Worker process entry point.  Also called by ``python -m novatrack.worker``."""
    import signal
    import threading

    from novatrack.config import Settings
    from novatrack.db import make_engine, make_session_factory
    from novatrack.providers.track17 import Track17Provider

    cfg = Settings()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    engine = make_engine(cfg.database_url)
    sf = make_session_factory(engine)
    provider = Track17Provider(cfg.track17_api_key.get_secret_value())

    stop_event = threading.Event()

    def _handle_stop(signum, frame) -> None:  # noqa: ANN001
        stop_event.set()
        logger.info("Worker shutting down (signal %s)", signum)

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    logger.info("Worker started (poll=%ss)", cfg.worker_poll_seconds)
    while not stop_event.is_set():
        try:
            tick(sf, cfg, provider)
        except Exception as exc:
            logger.error("Tick failed unexpectedly: %s", type(exc).__name__)
        stop_event.wait(cfg.worker_poll_seconds)

    # Graceful provider close (available on providers that wrap a connection pool)
    if hasattr(provider, "close"):
        try:
            provider.close()
        except Exception:
            pass
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
