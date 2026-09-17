"""One sync at a time, across processes.

The dashboard already guarded its own syncs with a threading.Lock, which
says nothing to a `hrusha sync` typed in a terminal: both processes then
write snapshots seconds apart and the overview counts every position
twice. The lock therefore has to live where both can see it — the ledger.

A holder row in `sync_state` is claimed under BEGIN IMMEDIATE, SQLite's
write lock, so two processes racing to claim it cannot both win. The
holder heartbeats while it works; a lock whose heartbeat has gone quiet
for LOCK_STALE_SECONDS is presumed dead (SIGKILL, power cut, container
stop) and taken over. Without that, one crash would block every future
sync until somebody deleted a row by hand.

The heartbeat runs on its own connection: a sqlite3 connection belongs
to the thread that made it, and the sync's own connection is busy doing
the sync.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("hrusha.sync_lock")

SYNC_LOCK_KEY = "sync_lock"
HEARTBEAT_SECONDS = 15
# a holder that has not beaten in this long is presumed dead. Generous
# next to the heartbeat: a machine under load must not lose its lock to
# a slow scheduler thread, and syncs are minutes at worst.
LOCK_STALE_SECONDS = 90


@dataclass(frozen=True)
class LockHolder:
    """Who is syncing, for logs and for telling the operator."""

    run_id: str
    who: str  # 'cli' | 'dashboard' | 'scheduler' — how the sync was triggered
    pid: int
    host: str
    started_ts: int
    heartbeat_ts: int

    def age_seconds(self, now: int | None = None) -> int:
        return max(0, (int(time.time()) if now is None else now) - self.started_ts)

    def describe(self, now: int | None = None) -> str:
        """One line an operator can act on: who, where, how long."""
        age = self.age_seconds(now)
        where = f"pid {self.pid}"
        if self.host != _host():
            where = f"{where} on {self.host}"
        return f"{self.who} ({where}), running {_duration(age)}"

    def is_stale(self, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        return now - self.heartbeat_ts > LOCK_STALE_SECONDS


class SyncBusy(Exception):
    """Another sync holds the lock. Carries the holder so callers can say who."""

    def __init__(self, holder: LockHolder) -> None:
        super().__init__(f"a sync is already running: {holder.describe()}")
        self.holder = holder


def _host() -> str:
    return socket.gethostname()


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m{seconds:02d}s"


def _connect(db_path: Path) -> sqlite3.Connection:
    """A short-lived autocommit connection for lock bookkeeping.

    isolation_level=None hands us explicit transaction control, which the
    claim needs: Python's implicit BEGIN would start a deferred (read)
    transaction and two processes could both pass the staleness check.
    """
    conn = sqlite3.connect(db_path, timeout=LOCK_STALE_SECONDS)
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _read_holder(conn: sqlite3.Connection) -> LockHolder | None:
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (SYNC_LOCK_KEY,)).fetchone()
    if row is None:
        return None
    try:
        return LockHolder(**json.loads(row[0]))
    except (ValueError, TypeError):  # hand-edited or written by an older build
        log.warning("unreadable sync lock row; treating as free")
        return None


def current_holder(db_path: Path, now: int | None = None) -> LockHolder | None:
    """The live holder, or None when free or stale. Never raises — the UI
    asks this on every page render and a lock problem must not blank the
    dashboard."""
    try:
        conn = _connect(db_path)
    except sqlite3.Error:
        return None
    try:
        holder = _read_holder(conn)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if holder is None or holder.is_stale(now):
        return None
    return holder


@contextmanager
def sync_lock(db_path: Path, who: str, run_id: str) -> Iterator[LockHolder]:
    """Hold the sync lock for the block, or raise SyncBusy.

    Released on the way out whatever happens, including failure — a
    crashed sync must not lock out the next one.
    """
    holder = _claim(db_path, who, run_id)
    stop = threading.Event()
    beat = threading.Thread(
        target=_heartbeat_loop, args=(db_path, run_id, stop), name="hrusha-sync-heartbeat"
    )
    beat.daemon = True
    beat.start()
    try:
        yield holder
    finally:
        stop.set()
        beat.join(timeout=HEARTBEAT_SECONDS)
        _release(db_path, run_id)


def _claim(db_path: Path, who: str, run_id: str) -> LockHolder:
    now = int(time.time())
    holder = LockHolder(
        run_id=run_id, who=who, pid=os.getpid(), host=_host(), started_ts=now, heartbeat_ts=now
    )
    conn = _connect(db_path)
    try:
        # BEGIN IMMEDIATE takes SQLite's write lock up front, so the
        # read-then-write below is atomic against another process doing
        # exactly the same thing a microsecond later.
        conn.execute("BEGIN IMMEDIATE")
        existing = _read_holder(conn)
        if existing is not None and not existing.is_stale(now):
            conn.execute("ROLLBACK")
            raise SyncBusy(existing)
        if existing is not None:
            log.warning(
                "taking over a stale sync lock",
                extra={
                    "stale_run_id": existing.run_id,
                    "stale_pid": existing.pid,
                    "silent_for": now - existing.heartbeat_ts,
                },
            )
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            (SYNC_LOCK_KEY, json.dumps(holder.__dict__)),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    log.info("sync lock acquired", extra={"run_id": run_id, "who": who})
    return holder


def _release(db_path: Path, run_id: str) -> None:
    """Drop the lock, but only if it is still ours: a stale-takeover may
    have handed it to someone else while we were stuck, and stealing it
    back would let two syncs run."""
    try:
        conn = _connect(db_path)
    except sqlite3.Error:
        log.warning("could not open the ledger to release the sync lock", extra={"run_id": run_id})
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = _read_holder(conn)
        if existing is not None and existing.run_id != run_id:
            conn.execute("ROLLBACK")
            log.warning(
                "sync lock was taken over while we held it; leaving it alone",
                extra={"run_id": run_id, "now_held_by": existing.run_id},
            )
            return
        conn.execute("DELETE FROM sync_state WHERE key = ?", (SYNC_LOCK_KEY,))
        conn.execute("COMMIT")
        log.info("sync lock released", extra={"run_id": run_id})
    except sqlite3.Error as exc:
        log.warning(
            "could not release the sync lock; it will go stale and be reclaimed",
            extra={"run_id": run_id, "why": type(exc).__name__},
        )
    finally:
        conn.close()


def _heartbeat_loop(db_path: Path, run_id: str, stop: threading.Event) -> None:
    """Say we are alive until the sync ends. Own connection, own thread."""
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            conn = _connect(db_path)
        except sqlite3.Error:
            continue  # transient; the next beat may well succeed
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = _read_holder(conn)
            if existing is None or existing.run_id != run_id:
                conn.execute("ROLLBACK")
                return  # no longer ours: stop beating rather than fight for it
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
                (
                    SYNC_LOCK_KEY,
                    json.dumps({**existing.__dict__, "heartbeat_ts": int(time.time())}),
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            pass  # a missed beat is survivable; LOCK_STALE_SECONDS allows several
        finally:
            conn.close()
