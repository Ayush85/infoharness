"""Background worker: claims queued scans from the database and runs them.

Run more workers for more throughput:  docker compose up -d --scale worker=2
"""
from __future__ import annotations

import logging
import os
import signal
import time
from datetime import timedelta

from sqlalchemy import select

from app import config
from app.db import Scan, init_engine, session_scope, utcnow
from app.pipeline import run_scan
from app import monitor

log = logging.getLogger("infoharness.worker")
logging.getLogger("httpx").setLevel(logging.WARNING)
_stop = False


def _handle_stop(*_):
    global _stop
    _stop = True


def recover_stuck() -> int:
    """Scans left 'running' by a crashed worker are marked failed so users aren't stuck waiting."""
    cutoff = utcnow() - timedelta(seconds=max(config.settings.max_scan_seconds, config.settings.pro_scan_seconds) * 2)
    n = 0
    with session_scope() as s:
        for scan in s.scalars(select(Scan).where(Scan.status == "running")):
            started = scan.started_at
            if started is not None and started.tzinfo is None:
                started = started.replace(tzinfo=cutoff.tzinfo)
            if started is None or started < cutoff:
                scan.status, scan.stage, scan.error = "failed", "failed", "The scan was interrupted. Please try again."
                scan.finished_at = utcnow()
                n += 1
    return n


def claim_next() -> str | None:
    with session_scope() as s:
        stmt = (select(Scan).where(Scan.status == "queued").order_by(Scan.created_at)
                .limit(1).with_for_update(skip_locked=True))
        scan = s.scalars(stmt).first()
        if scan is None:
            return None
        scan.status, scan.stage, scan.started_at = "running", "starting", utcnow()
        return scan.id


def run_once() -> bool:
    scan_id = claim_next()
    if not scan_id:
        return False
    log.info("scan %s started", scan_id)
    run_scan(scan_id)
    log.info("scan %s finished", scan_id)
    try:
        monitor.after_scan(scan_id)
    except Exception:
        log.exception("post-scan hook failed for %s", scan_id)
    return True


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    init_engine()
    recovered = recover_stuck()
    if recovered:
        log.warning("marked %d interrupted scans as failed", recovered)
    log.info("worker ready (llm=%s, embeddings=%s)", config.settings.llm_enabled, config.settings.embeddings_provider)
    last_sched = 0.0
    last_digest = 0.0
    while not _stop:
        try:
            if time.monotonic() - last_sched > 60:
                last_sched = time.monotonic()
                queued = monitor.queue_due_scans()
                if queued:
                    log.info("queued %d scheduled rescans", queued)
            if time.monotonic() - last_digest > 3600:
                last_digest = time.monotonic()
                if monitor.send_digests():
                    log.info("sent weekly digests")
            if not run_once():
                time.sleep(config.settings.worker_poll_seconds)
        except Exception:
            log.exception("worker loop error")
            time.sleep(5)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
