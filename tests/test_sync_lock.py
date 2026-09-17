"""One sync at a time, across processes.

The lock lives in the ledger precisely so a `hrusha sync` in a terminal
and the dashboard's scheduler can see each other, so the interesting
cases are contention, crash recovery, and never locking yourself out.
"""

import json
import multiprocessing
import sqlite3
import time

import pytest

from hrusha.ledger.store import open_ledger
from hrusha.ledger.sync_lock import (
    LOCK_STALE_SECONDS,
    SYNC_LOCK_KEY,
    LockHolder,
    SyncBusy,
    current_holder,
    sync_lock,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ledger.db"
    open_ledger(path).close()  # create the schema the lock row lives in
    return path


def stored_holder(db_path) -> LockHolder | None:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM sync_state WHERE key = ?", (SYNC_LOCK_KEY,)
        ).fetchone()
    finally:
        conn.close()
    return LockHolder(**json.loads(row[0])) if row else None


def backdate(db_path, seconds: int) -> None:
    """Age the holder's heartbeat, as if its process had stopped beating."""
    holder = stored_holder(db_path)
    aged = {**holder.__dict__, "heartbeat_ts": int(time.time()) - seconds}
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "UPDATE sync_state SET value = ? WHERE key = ?", (json.dumps(aged), SYNC_LOCK_KEY)
        )
    conn.close()


# -- the basics --------------------------------------------------------------


def test_lock_is_taken_and_released(db_path):
    with sync_lock(db_path, "cli", "run-1") as holder:
        assert holder.who == "cli"
        assert current_holder(db_path).run_id == "run-1"
    assert current_holder(db_path) is None
    assert stored_holder(db_path) is None  # row gone, not just stale


def test_second_sync_is_refused_while_the_first_runs(db_path):
    with sync_lock(db_path, "cli", "run-1"):
        with pytest.raises(SyncBusy) as caught:
            with sync_lock(db_path, "dashboard", "run-2"):
                raise AssertionError("must not get the lock")
    assert caught.value.holder.run_id == "run-1"
    assert caught.value.holder.who == "cli"  # the message can name the culprit


def test_lock_is_released_when_the_sync_fails(db_path):
    """A crashed sync must not lock out every sync that follows."""
    with pytest.raises(ZeroDivisionError):
        with sync_lock(db_path, "cli", "run-1"):
            1 / 0  # noqa: B018
    assert current_holder(db_path) is None
    with sync_lock(db_path, "cli", "run-2"):
        pass  # the next one gets it


# -- crash recovery ----------------------------------------------------------


def test_a_silent_holder_goes_stale_and_is_taken_over(db_path):
    """SIGKILL, power cut, `docker stop` — nobody runs the release."""
    with sync_lock(db_path, "dashboard", "run-1"):
        backdate(db_path, LOCK_STALE_SECONDS + 1)
        assert current_holder(db_path) is None  # presumed dead
        with sync_lock(db_path, "cli", "run-2") as holder:
            assert holder.run_id == "run-2"


def test_a_holder_that_is_merely_slow_keeps_its_lock(db_path):
    with sync_lock(db_path, "dashboard", "run-1"):
        backdate(db_path, LOCK_STALE_SECONDS - 5)  # quiet, but not dead
        assert current_holder(db_path).run_id == "run-1"
        with pytest.raises(SyncBusy):
            with sync_lock(db_path, "cli", "run-2"):
                pass


def test_a_taken_over_lock_is_not_stolen_back_on_release(db_path):
    """If we were presumed dead and someone took over, our release must
    leave their lock alone — otherwise two syncs run at once."""
    with sync_lock(db_path, "dashboard", "run-1"):
        backdate(db_path, LOCK_STALE_SECONDS + 1)
        with sync_lock(db_path, "cli", "run-2"):
            pass  # run-2 takes over, then releases cleanly
        # run-1's context exits here and must not delete a row it lost
        conn = sqlite3.connect(db_path)
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
                (SYNC_LOCK_KEY, json.dumps(_holder_dict("run-3"))),
            )
        conn.close()
    assert current_holder(db_path).run_id == "run-3"  # run-1 left it alone


def _holder_dict(run_id: str) -> dict:
    now = int(time.time())
    return {
        "run_id": run_id,
        "who": "cli",
        "pid": 1234,
        "host": "somewhere",
        "started_ts": now,
        "heartbeat_ts": now,
    }


# -- robustness --------------------------------------------------------------


def test_unreadable_lock_row_does_not_wedge_syncing(db_path):
    """A hand-edited or older-format row must not be a permanent lock."""
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?)", (SYNC_LOCK_KEY, "not json")
        )
    conn.close()
    assert current_holder(db_path) is None
    with sync_lock(db_path, "cli", "run-1") as holder:
        assert holder.run_id == "run-1"


def test_current_holder_survives_a_missing_database(tmp_path):
    """The UI calls this on every render; it must never blank the page."""
    assert current_holder(tmp_path / "nope.db") is None


def test_describe_names_who_and_how_long(db_path):
    with sync_lock(db_path, "cli", "run-1") as holder:
        described = holder.describe(now=holder.started_ts + 75)
    assert "cli" in described
    assert "1m15s" in described


# -- across real processes ---------------------------------------------------


def _try_claim(db_path, run_id, results):
    from hrusha.ledger.sync_lock import SyncBusy as Busy
    from hrusha.ledger.sync_lock import sync_lock as lock

    try:
        with lock(db_path, "cli", run_id):
            results.put(("got", run_id))
            time.sleep(1.5)
    except Busy:
        results.put(("busy", run_id))


def test_two_processes_cannot_both_sync(db_path):
    """The whole point: a threading.Lock cannot do this."""
    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    first = ctx.Process(target=_try_claim, args=(str(db_path), "proc-1", results))
    first.start()
    time.sleep(0.6)  # let it claim
    second = ctx.Process(target=_try_claim, args=(str(db_path), "proc-2", results))
    second.start()
    first.join(20)
    second.join(20)

    outcomes = {}
    for _ in range(2):
        outcome, run_id = results.get(timeout=5)
        outcomes[run_id] = outcome
    assert outcomes == {"proc-1": "got", "proc-2": "busy"}
    assert current_holder(db_path) is None  # both processes cleaned up
