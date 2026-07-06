"""Local rules/manual-tags backup: export -> wipe -> import round-trip."""

from decimal import Decimal

from hrusha.ledger.ingest import ingest_transfers
from hrusha.ledger.rules_io import export_local, import_local
from hrusha.ledger.store import open_ledger
from hrusha.ledger.tags import add_rule, set_manual_tag
from tests.conftest import OUTSIDER, make_transfer


def no_price(token_key, ts):
    return None


def seed(conn):
    ingest_transfers(conn, [make_transfer()], tracked_addresses=set(), price_fn=no_price)
    add_rule(conn, 40, {"counterparty": OUTSIDER, "direction": "in"}, ["swap"], source=None)
    add_rule(conn, 90, {"token": "SPAM"}, ["ignore"], source="spam")
    with conn:
        conn.execute("UPDATE tag_rules SET enabled = 0 WHERE priority = 90")
    event_id = conn.execute("SELECT id FROM events").fetchone()[0]
    set_manual_tag(conn, event_id, "venft-purchase")


def test_round_trip_restores_rules_and_manual_tags(ledger, tmp_path):
    seed(ledger)
    path = tmp_path / "rules.yaml"
    stats = export_local(ledger, path)
    assert (stats.rules, stats.manual_tags) == (2, 1)
    assert (path.stat().st_mode & 0o777) == 0o600

    # simulate the disaster: a wiped DB, re-synced from chain
    fresh = open_ledger(tmp_path / "fresh.db")
    ingest_transfers(fresh, [make_transfer()], tracked_addresses=set(), price_fn=no_price)

    imported = import_local(fresh, path)
    assert imported.rules_added == 2
    assert imported.tags_added == 1
    assert imported.tags_missing_event == 0
    enabled_by_priority = dict(fresh.execute("SELECT priority, enabled FROM tag_rules").fetchall())
    assert enabled_by_priority == {40: 1, 90: 0}
    assert fresh.execute("SELECT tag, origin FROM tags WHERE origin = 'manual'").fetchall() == [
        ("venft-purchase", "manual")
    ]
    fresh.close()


def test_import_is_idempotent(ledger, tmp_path):
    seed(ledger)
    path = tmp_path / "rules.yaml"
    export_local(ledger, path)

    again = import_local(ledger, path)
    assert again.rules_added == 0
    assert again.rules_existing == 2
    assert ledger.execute("SELECT COUNT(*) FROM tag_rules").fetchone() == (2,)


def test_manual_tag_for_unsynced_event_is_skipped_and_counted(ledger, tmp_path):
    seed(ledger)
    path = tmp_path / "rules.yaml"
    export_local(ledger, path)

    empty = open_ledger(tmp_path / "empty.db")  # tag's event not synced yet
    stats = import_local(empty, path)
    assert stats.tags_missing_event == 1
    assert stats.tags_added == 0
    empty.close()


def test_export_amounts_are_not_needed_for_identity(ledger, tmp_path):
    # identity is (tx_hash, log_index, kind): amount changes (re-ingest quirks)
    # must not orphan the tag
    seed(ledger)
    path = tmp_path / "rules.yaml"
    export_local(ledger, path)

    fresh = open_ledger(tmp_path / "fresh.db")
    ingest_transfers(
        fresh,
        [make_transfer(amount=Decimal("999"))],
        tracked_addresses=set(),
        price_fn=no_price,
    )
    assert import_local(fresh, path).tags_added == 1
    fresh.close()
