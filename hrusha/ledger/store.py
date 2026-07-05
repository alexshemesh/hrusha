"""SQLite ledger: schema v1 and forward-only migrations.

The database is derived state (rebuildable from chain), kept in a
mounted /data volume in production and ~/.hrusha/hrusha.db natively.
Schema version lives in SQLite's `PRAGMA user_version`; migrations are
append-only — never edit an entry in SCHEMA_MIGRATIONS after it has
shipped, add a new one.

Amounts are stored as exact decimal strings (`amount_native`), never
floats: token amounts exceed float precision (18-decimals wei).
USD valuations are analytical, so REAL is acceptable there.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_MIGRATIONS: tuple[str, ...] = (
    # v1 — initial schema per docs/DESIGN.md ledger event model
    """
    CREATE TABLE events (
        id            INTEGER PRIMARY KEY,
        ts            INTEGER NOT NULL,             -- unix seconds, event time on chain
        chain         TEXT    NOT NULL,             -- 'base' in v1
        tx_hash       TEXT    NOT NULL,
        log_index     INTEGER NOT NULL DEFAULT -1,  -- -1 for tx-level events (gas_fee)
        block         INTEGER NOT NULL,
        kind          TEXT    NOT NULL,             -- transfer_in|transfer_out|gas_fee|
                                                    --   reward_claim|swap|vote
        token         TEXT    NOT NULL,             -- token symbol or contract address
        amount_native TEXT    NOT NULL,             -- exact decimal string
        usd_at_time   REAL,
        gas_native    TEXT,
        gas_usd       REAL,
        address       TEXT    NOT NULL,             -- tracked address this event belongs to
        source        TEXT,                         -- aerodrome-voting|morpho|40acres|...
        epoch_id      TEXT,
        UNIQUE (tx_hash, log_index, kind)           -- ingestion dedup, re-runs are idempotent
    );
    CREATE INDEX idx_events_ts ON events (ts);
    CREATE INDEX idx_events_source_epoch ON events (source, epoch_id);
    CREATE INDEX idx_events_address ON events (address);

    CREATE TABLE snapshots (
        id            INTEGER PRIMARY KEY,
        ts            INTEGER NOT NULL,
        chain         TEXT    NOT NULL,
        address       TEXT    NOT NULL,
        kind          TEXT    NOT NULL,             -- balance|position|claimable
        token         TEXT    NOT NULL,
        source        TEXT,
        amount_native TEXT    NOT NULL,
        usd_at_time   REAL
    );
    CREATE INDEX idx_snapshots_ts ON snapshots (ts);

    CREATE TABLE tags (
        event_id INTEGER NOT NULL REFERENCES events (id) ON DELETE CASCADE,
        tag      TEXT    NOT NULL,
        origin   TEXT    NOT NULL DEFAULT 'rule',   -- rule|manual; manual always wins
        UNIQUE (event_id, tag)
    );

    CREATE TABLE tag_rules (
        id         INTEGER PRIMARY KEY,
        priority   INTEGER NOT NULL,
        match_json TEXT    NOT NULL,                -- {counterparty|token|kind|direction: ...}
        tags       TEXT    NOT NULL,                -- comma-separated tags to apply
        source     TEXT,                            -- source to assign, if any
        enabled    INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE epochs (
        epoch_id TEXT PRIMARY KEY,                  -- e.g. '2026-07-02' (flip date, Thu 00:00 UTC)
        starts_ts INTEGER NOT NULL,
        ends_ts   INTEGER NOT NULL
    );

    CREATE TABLE sync_state (
        key   TEXT PRIMARY KEY,                     -- e.g. 'transfers_cursor:<address>'
        value TEXT NOT NULL
    );
    """,
)

SCHEMA_VERSION = len(SCHEMA_MIGRATIONS)


def open_ledger(db_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the ledger DB, migrated to the latest schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn)
    return conn


def apply_migrations(conn: sqlite3.Connection) -> None:
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"ledger schema version {current} is newer than this build supports "
            f"({SCHEMA_VERSION}); upgrade hrusha instead of downgrading the database"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        conn.executescript(SCHEMA_MIGRATIONS[version - 1])
        conn.execute(f"PRAGMA user_version = {int(version)}")
        conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])
