---
date: 2026-09-14T15:20
type: fix
status: implemented
trigger: bug
touches:
  - hrusha/ledger/sync_lock.py
  - hrusha/ledger/store.py
  - hrusha/ledger/reports.py
  - hrusha/service/sync.py
  - hrusha/service/app.py
  - hrusha/service/templates/base.html
  - hrusha/cli.py
related:
  - 2026-07-09-1317-sync-caching-and-claimables-fix.md
supersedes: null
commit: null
pr: null
---

# One sync at a time, and snapshots that know which sync wrote them

## Context

The overview showed every position three times — `aerodrome-voting AERO
27,613.705852` on three consecutive rows — and holdings of $56k against
a real ~$18.7k, which turned a roughly −$1.8k profit line into +$35.7k.

Two independent defects stacked:

1. **Nothing stopped concurrent syncs.** The dashboard guarded its
   syncs with a `threading.Lock`, which says nothing to a `hrusha sync`
   typed in a terminal. The operator ran one while `hrusha serve` was
   up; its scheduler fires 20s after boot and hourly after that. Both
   processes wrote a full set of snapshots.
2. **"Latest snapshots" was a time window.** One sync writes its four
   snapshot groups (balances, aerodrome, morpho, 40acres) seconds apart,
   each with its own `now()`, so `latest_snapshots` took every row
   within `SNAPSHOT_SYNC_WINDOW_SECONDS = 600` of the newest. Two syncs
   inside ten minutes are indistinguishable from one — and they need not
   even overlap, so fixing (1) alone would not have fixed the display.

The ledger itself was never wrong. Every row was a truthful record of a
real sync; the read merged runs that had nothing to do with each other.

## Problem / Goal

Make a second sync impossible while one is running, say so in the UI,
and make "the current state" mean exactly one sync's worth of rows.

## Decision

1. **The lock lives in the ledger, not in a process.** A holder row in
   `sync_state` (`sync_lock`) claimed under `BEGIN IMMEDIATE` — SQLite's
   write lock — so two processes racing to claim cannot both win. The
   row carries run_id, who ('cli'/'dashboard'/'scheduler'), pid, host,
   started_ts, heartbeat_ts, which is what lets the UI and the CLI say
   *who* is syncing rather than just "busy".
2. **Heartbeat, not a timeout.** The holder beats every 15s on its own
   connection; a lock silent for 90s is presumed dead and taken over. A
   SIGKILL, a power cut or a `docker stop` must not lock out every
   future sync, and no TTL guessed from "how long is a sync" survives a
   first-run backfill. Release is skipped if the lock is no longer ours:
   a takeover while we were stuck must not let two syncs run.
3. **Guard inside `run_full_sync`**, not at each call site — every
   caller is covered, including ones not written yet. It raises
   `SyncBusy`, carrying the holder.
4. **Refusal is not failure.** The CLI prints who holds the lock and
   exits 6, saying plainly that nothing was changed. The dashboard's
   `run_sync_once` treats it as a skip, not an outage, so the
   scheduler's backoff does not trip on a healthy system.
5. **Schema v6: `snapshots.sync_run_id`.** The four writers stamp it,
   `latest_snapshots` selects the newest row's run, and the 600-second
   window is deleted. Pre-v6 rows get `'pre-v6-<ts>'` so historical
   groups stay separable without inventing accuracy we do not have.
6. **The UI shows the truth.** `page()` asks the ledger for the current
   holder, so a CLI-driven sync disables the refresh button too, with a
   banner naming the holder and a 10s meta-refresh so the page comes
   back by itself.

## Alternatives Considered

- **Dedupe snapshots by (address, kind, token, source)** — rejected,
  and worth recording why: several veNFTs on one address produce rows
  with an identical key (the 27,613 / 5,408 / 669 AERO rows are three
  separate locks), and the table has no veNFT column. This "one-line
  fix" would have silently shrunk the operator's real position. A test
  pins it.
- **A PID file / flock** — rejected: the ledger is the one thing every
  participant already opens, and a bind-mounted `/data` makes file
  locking semantics across container and host a coin toss.
- **Keep the window, widen or narrow it** — no width is correct. Two
  syncs can be one second apart or one group can straddle a minute.
- **A lock table with a row per run and `state`** — more schema for no
  gain; one row is the whole state.
- **Making the scheduler skip when the ledger changed recently** —
  treats the symptom, and still allows two `hrusha sync` runs.

## Implementation Notes

- The heartbeat thread opens its own connection: a sqlite3 connection
  belongs to the thread that created it, and the sync's own connection
  is busy syncing.
- `_connect` sets `isolation_level = None`; Python's implicit `BEGIN`
  is deferred (a read transaction), under which two processes could
  both pass the staleness check before either wrote.
- `current_holder` never raises — the UI calls it on every render, and
  a lock problem must not blank the dashboard.
- An unreadable lock row (hand-edited, or written by an older build) is
  treated as free rather than as a permanent lock.
- `tests/test_sync_lock.py` includes a real two-process contention test
  via `multiprocessing` — a `threading.Lock` passes every single-process
  test, which is exactly how this shipped broken.

## Follow-ups

- The scout and invest scans still guard only in-process. They are
  read-mostly, so concurrent runs waste work rather than corrupt data,
  but the same lock would fit with a different key.
- `hrusha sync --force` to break a lock deliberately, if a wedged lock
  ever outlives its staleness window in practice.
- Snapshots accumulate a set per sync forever; a retention policy
  (keep N runs, or one per day beyond a week) is now easy to express
  because runs are identifiable.
