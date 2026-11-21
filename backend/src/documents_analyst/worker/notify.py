"""The notify listener — a latency hint, never a source of truth.

One daemon thread holds `LISTEN analyst_work` and sets a `threading.Event`. It
carries no pile id and queues no work. Deliberately that weak: the main loop
reconciles every tick — the database is the truth — so a listener that *delivered*
work would make a missed notification a lost pile, and notifications are missed
routinely. Here a missed one costs up to `SCAN_INTERVAL_S` of latency, nothing
more. The channel's publishers (the `pending_decisions` trigger, the upload
route) all mean the same thing: something changed, look sooner.

See docs/WORKER_RUNTIME.md § "What wakes the scanner".
"""

from __future__ import annotations

import logging
import threading

import psycopg

LOG = logging.getLogger(__name__)

WORK_CHANNEL = "analyst_work"


def start_listener(
    dsn: str, wake: threading.Event, stop: threading.Event, *, channel: str = WORK_CHANNEL
) -> threading.Thread:
    """Start the listener. Returns the thread so a test can join it. A daemon
    thread: it holds one idle connection and no state worth draining, so a shutdown
    waiting for it to notice would wait for nothing."""
    thread = threading.Thread(
        target=_listen_forever,
        args=(dsn, wake, stop, channel),
        name="notify-listener",
        daemon=True,
    )
    thread.start()
    return thread


def _listen_forever(dsn: str, wake: threading.Event, stop: threading.Event, channel: str) -> None:
    delay = 0.5
    while not stop.is_set():
        try:
            _listen_once(dsn, wake, stop, channel)
            delay = 0.5
        except Exception as exc:  # noqa: BLE001 — a dropped listener retries; it never kills the worker
            LOG.warning("notify listener lost (%s: %s); retrying", type(exc).__name__, exc)
            stop.wait(delay)
            delay = min(delay * 2, 10.0)


def _listen_once(dsn: str, wake: threading.Event, stop: threading.Event, channel: str) -> None:
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        conn.execute(f"LISTEN {channel}")
        LOG.info("listening on %s", channel)
        # Anything published while we were reconnecting is gone. Waking once on
        # connect turns that hole into one early scan.
        wake.set()
        while not stop.is_set():
            # Bounded wait, not open-ended, so SIGTERM is noticed within a second
            # instead of whenever the next notification arrives — which on a quiet
            # system is never. The connection is kept across iterations.
            for _ in conn.notifies(timeout=1.0):
                wake.set()
    finally:
        conn.close()
