"""Snapshots belong to a sync run, not a time window (schema v6).

The window this replaces counted every position once per sync whenever
two syncs landed within 600 seconds — which is exactly what a manual
`hrusha sync` next to the dashboard's scheduler does.
"""

import sqlite3

from hrusha.ledger import reports
from hrusha.ledger.store import SCHEMA_MIGRATIONS, SCHEMA_VERSION, open_ledger, schema_version

MAIN = "0x" + "1" * 40


def add_snapshot(
    conn, run_id, ts, token="AERO", amount="100", usd=100.0, source="aerodrome-voting"
):
    conn.execute(
        """
        INSERT INTO snapshots (ts, chain, address, kind, token, source,
                               amount_native, usd_at_time, sync_run_id)
        VALUES (?, 'base', ?, 'position', ?, ?, ?, ?, ?)
        """,
        (ts, MAIN, token, source, amount, usd, run_id),
    )


def test_two_syncs_in_one_window_do_not_merge(ledger):
    """The bug, directly: same positions, two runs, 90 seconds apart."""
    for run_id, ts in (("run-old", 1_000_000), ("run-new", 1_000_090)):
        add_snapshot(ledger, run_id, ts, amount="27613.7", usd=15369.20)
        add_snapshot(ledger, run_id, ts + 2, token="USDC", amount="4087.5", usd=4086.9)
    ledger.commit()

    rows = reports.latest_snapshots(ledger)

    assert len(rows) == 2  # one run's worth, not both
    assert {row.token for row in rows} == {"AERO", "USDC"}
    assert sum(row.usd_at_time for row in rows) == 15369.20 + 4086.9


def test_groups_written_seconds_apart_stay_together(ledger):
    """One sync writes balances/aerodrome/morpho/40acres with separate
    now()s — the run id is what holds them together."""
    add_snapshot(ledger, "run-1", 1_000_000, token="AERO")
    add_snapshot(ledger, "run-1", 1_000_014, token="USDC")
    add_snapshot(ledger, "run-1", 1_000_031, token="WETH")
    ledger.commit()
    assert len(reports.latest_snapshots(ledger)) == 3


def test_several_venfts_on_one_address_are_all_kept(ledger):
    """Three veNFTs produce three rows with an identical key — dedup by
    (address, kind, token, source) would silently delete real positions."""
    for amount, usd in (("27613.7", 15369.20), ("5408.3", 3010.15), ("669.7", 372.77)):
        add_snapshot(ledger, "run-1", 1_000_000, amount=amount, usd=usd)
    ledger.commit()

    rows = reports.latest_snapshots(ledger)

    assert len(rows) == 3
    assert sum(row.usd_at_time for row in rows) == 15369.20 + 3010.15 + 372.77


def test_empty_ledger_has_no_latest(ledger):
    assert reports.latest_snapshots(ledger) == []


def test_migration_keeps_old_snapshots_separable(tmp_path):
    """Rows written before v6 have no run id; each historical group gets a
    synthetic one from its ts so they cannot merge either."""
    db_path = tmp_path / "ledger.db"
    conn = sqlite3.connect(db_path)
    for migration in SCHEMA_MIGRATIONS[:5]:
        conn.executescript(migration)
    conn.execute("PRAGMA user_version = 5")
    for ts in (1_000_000, 1_000_090):  # two pre-v6 syncs inside the old window
        conn.execute(
            """
            INSERT INTO snapshots (ts, chain, address, kind, token, source,
                                   amount_native, usd_at_time)
            VALUES (?, 'base', ?, 'position', 'AERO', 'aerodrome-voting', '100', 100.0)
            """,
            (ts, MAIN),
        )
    conn.commit()
    conn.close()

    migrated = open_ledger(db_path)
    assert schema_version(migrated) == SCHEMA_VERSION == 6
    rows = reports.latest_snapshots(migrated)
    assert len(rows) == 1  # the newer group only
    assert rows[0].ts == 1_000_090
    migrated.close()
