from decimal import Decimal

from hrusha.ledger.ingest import ingest_fees, ingest_transfers
from tests.conftest import COLD, MAIN, TX_OWN, make_fee, make_transfer

TRACKED = {MAIN, COLD}


def flat_price(token_key, ts):
    return Decimal("2.0")


def no_price(token_key, ts):
    return None


def test_ingest_is_idempotent(ledger):
    transfers = [make_transfer(), make_transfer(log_index=8)]
    first = ingest_transfers(ledger, transfers, TRACKED, flat_price)
    second = ingest_transfers(ledger, transfers, TRACKED, flat_price)
    assert first.events_inserted == 2
    assert second.events_inserted == 0
    assert second.events_skipped == 2
    assert ledger.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2


def test_usd_at_time_computed_or_null(ledger):
    ingest_transfers(ledger, [make_transfer(amount=Decimal("100.5"))], TRACKED, flat_price)
    ingest_transfers(ledger, [make_transfer(log_index=8)], TRACKED, no_price)
    priced, unpriced = ledger.execute(
        "SELECT usd_at_time FROM events ORDER BY log_index"
    ).fetchall()
    assert priced[0] == 201.0  # 100.5 * 2.0
    assert unpriced[0] is None


def test_own_transfer_tagged(ledger):
    own = make_transfer(tx_hash=TX_OWN, direction="out", counterparty=COLD)
    outside = make_transfer(log_index=8)
    stats = ingest_transfers(ledger, [own, outside], TRACKED, flat_price)
    assert stats.own_transfers_tagged == 1
    tagged = ledger.execute(
        "SELECT e.tx_hash FROM tags t JOIN events e ON e.id = t.event_id "
        "WHERE t.tag = 'own-transfer' AND t.origin = 'rule'"
    ).fetchall()
    assert tagged == [(TX_OWN,)]


def test_fee_ingest_dedups_and_prices(ledger):
    fees = [make_fee(amount_eth=Decimal("0.0002"))]
    ts_by_tx = {fees[0].tx_hash: 1_750_000_000}
    first = ingest_fees(ledger, fees, ts_by_tx, flat_price)
    second = ingest_fees(ledger, fees, ts_by_tx, flat_price)
    assert (first.events_inserted, second.events_inserted) == (1, 0)
    row = ledger.execute(
        "SELECT kind, token, amount_native, gas_native, gas_usd, ts FROM events"
    ).fetchone()
    assert row == ("gas_fee", "ETH", "0.0002", "0.0002", 0.0004, 1_750_000_000)
