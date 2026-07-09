"""Background sync scheduler for `hrusha serve`.

Hourly sync, tightened to every 15 minutes in the window around the
Aerodrome epoch flip (Thursday 00:00 UTC): the 4 hours BEFORE the flip
are when votes and rewards move fastest toward the operator's vote
cutoff, and the 2 hours AFTER are when new claimables appear.

Wakeups carry a small jitter so restarted instances don't sync in
lockstep with anything else on the hour. Failures are logged and
retried with exponential backoff (5 min doubling, capped at 1 h) —
the scheduler must NEVER crash the service. It runs the dashboard's
own guarded sync, so a scheduled run and a manual refresh can never
overlap: whoever is second simply skips.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable

from hrusha.ledger.tags import SECONDS_PER_WEEK

log = logging.getLogger("hrusha.scheduler")

BASE_INTERVAL_SECONDS = 3600
FAST_INTERVAL_SECONDS = 900
FAST_BEFORE_FLIP_SECONDS = 4 * 3600
FAST_AFTER_FLIP_SECONDS = 2 * 3600
JITTER_SECONDS = 120
STARTUP_DELAY_SECONDS = 20  # first sync shortly after boot: fresh data fast
RETRY_BASE_SECONDS = 300
RETRY_CAP_SECONDS = 3600


def next_delay(now: int, consecutive_failures: int = 0, jitter: int | None = None) -> int:
    """Seconds until the next sync attempt. Pure — the testable cadence."""
    if jitter is None:
        jitter = secrets.randbelow(JITTER_SECONDS)  # crypto-safe silences S311; speed is moot
    if consecutive_failures:
        backoff = RETRY_BASE_SECONDS * 2 ** (consecutive_failures - 1)
        return min(backoff, RETRY_CAP_SECONDS) + jitter
    since_flip = now % SECONDS_PER_WEEK  # weeks are Thursday-anchored since epoch
    until_flip = SECONDS_PER_WEEK - since_flip
    fast = until_flip <= FAST_BEFORE_FLIP_SECONDS or since_flip <= FAST_AFTER_FLIP_SECONDS
    return (FAST_INTERVAL_SECONDS if fast else BASE_INTERVAL_SECONDS) + jitter


class SyncScheduler:
    """Daemon thread driving `run_sync` on the cadence above.

    `run_sync` is the app's guarded one-at-a-time sync; it returns False
    on failure and True on success or busy-skip, and never raises."""

    def __init__(self, run_sync: Callable[[], bool]) -> None:
        self._run_sync = run_sync
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.next_run_ts = 0  # dashboard shows "auto-sync ~HH:MM"
        self.consecutive_failures = 0

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="hrusha-sync-scheduler", daemon=True
        )
        self._thread.start()
        log.info("sync scheduler started")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        delay = STARTUP_DELAY_SECONDS + secrets.randbelow(JITTER_SECONDS)
        while True:
            self.next_run_ts = int(time.time()) + delay
            if self._stop.wait(delay):
                return
            try:
                ok = self._run_sync()
            except Exception:  # belt and suspenders: never let the loop die
                log.exception("scheduled sync raised unexpectedly")
                ok = False
            self.consecutive_failures = 0 if ok else self.consecutive_failures + 1
            if self.consecutive_failures:
                log.warning(
                    "scheduled sync failing",
                    extra={"consecutive_failures": self.consecutive_failures},
                )
            delay = next_delay(int(time.time()), self.consecutive_failures)
