import sqlite3

import pytest

from hrusha.ledger.store import SCHEMA_VERSION, open_ledger, schema_version

EXPECTED_TABLES = {"events", "snapshots", "tags", "tag_rules", "epochs", "sync_state"}


@pytest.fixture
def ledger(tmp_path):
    conn = open_ledger(tmp_path / "ledger.db")
    yield conn
    conn.close()


def table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def test_fresh_db_migrates_to_latest(ledger):
    assert schema_version(ledger) == SCHEMA_VERSION
    assert EXPECTED_TABLES <= table_names(ledger)


def test_reopen_is_idempotent(tmp_path):
    db_path = tmp_path / "ledger.db"
    open_ledger(db_path).close()
    conn = open_ledger(db_path)  # must not fail re-applying migrations
    assert schema_version(conn) == SCHEMA_VERSION
    conn.close()


def test_migration_from_v1_preserves_data(tmp_path):
    """Apply only migration 1, insert data, reopen -> v2 applied, data intact."""
    import sqlite3

    from hrusha.ledger.store import SCHEMA_MIGRATIONS

    db_path = tmp_path / "ledger.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_MIGRATIONS[0])
    conn.execute("PRAGMA user_version = 1")
    insert_event(conn)
    conn.commit()
    conn.close()

    migrated = open_ledger(db_path)
    assert schema_version(migrated) == SCHEMA_VERSION
    assert migrated.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    # v2 additions usable
    migrated.execute("SELECT counterparty, contract FROM events")
    migrated.execute("SELECT token, day, usd FROM price_cache")
    migrated.close()


def test_newer_schema_than_build_is_rejected(tmp_path):
    db_path = tmp_path / "ledger.db"
    conn = open_ledger(db_path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer than this build"):
        open_ledger(db_path)


def insert_event(conn, tx_hash="0xabc", log_index=0, kind="transfer_in"):
    conn.execute(
        """
        INSERT INTO events (ts, chain, tx_hash, log_index, block, kind, token,
                            amount_native, address)
        VALUES (?, 'base', ?, ?, 1, ?, 'ETH', '1.0', '0xowner')
        """,
        (1720000000, tx_hash, log_index, kind),
    )


def test_event_dedup_constraint(ledger):
    insert_event(ledger)
    with pytest.raises(sqlite3.IntegrityError):
        insert_event(ledger)  # same (tx_hash, log_index, kind)
    insert_event(ledger, log_index=1)  # different log_index is a distinct event
    insert_event(ledger, kind="gas_fee")  # different kind is a distinct event
    assert ledger.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3


def test_tag_cascade_on_event_delete(ledger):
    insert_event(ledger)
    event_id = ledger.execute("SELECT id FROM events").fetchone()[0]
    ledger.execute("INSERT INTO tags (event_id, tag) VALUES (?, 'own-transfer')", (event_id,))
    ledger.execute("DELETE FROM events WHERE id = ?", (event_id,))
    assert ledger.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 0
