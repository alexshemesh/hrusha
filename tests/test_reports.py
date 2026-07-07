"""strategy_summary: lifetime profit per strategy from ledger flows + snapshots."""

import time
from decimal import Decimal

from hrusha.ledger.ingest import ingest_transfers
from hrusha.ledger.reports import strategy_summary
from hrusha.ledger.tags import set_manual_tag
from tests.conftest import MAIN, OUTSIDER, TX_1, TX_2, make_transfer

VAULT = "0x" + "b" * 40
TX_3 = "0x" + "7" * 64
TX_4 = "0x" + "8" * 64


def usd(amount):
    """Seed convention: $1 per token unit, so USD == amount."""
    return lambda token, ts: Decimal(1)


def tag_events(conn, tag_by_txkind):
    for (tx, kind), tags in tag_by_txkind.items():
        event_id = conn.execute(
            "SELECT id FROM events WHERE tx_hash = ? AND kind = ?", (tx, kind)
        ).fetchone()[0]
        for tag in tags:
            set_manual_tag(conn, event_id, tag)


def seed_vault_strategy(ledger):
    """$100 deposited (with an unpriced share mint), $60 withdrawn, $10 claim,
    $1 gas, $50 still in the vault -> profit $19."""
    ingest_transfers(
        ledger,
        [
            make_transfer(tx_hash=TX_1, direction="out", amount=Decimal(100)),
            make_transfer(tx_hash=TX_2, log_index=8, amount=Decimal(60)),
            make_transfer(tx_hash=TX_3, log_index=9, amount=Decimal(10)),
        ],
        tracked_addresses=set(),
        price_fn=usd(1),
    )
    # unpriced share mint mirroring the deposit (swap-tagged IN leg)
    ingest_transfers(
        ledger,
        [
            make_transfer(
                tx_hash=TX_1,
                log_index=10,
                direction="in",
                token="VAULT",
                contract=VAULT,
                amount=Decimal(2),
            )
        ],
        tracked_addresses=set(),
        price_fn=lambda token, ts: None,
    )
    with ledger:
        ledger.execute("UPDATE events SET source = 'vaultco' WHERE kind != 'gas_fee'")
        ledger.execute(
            "INSERT INTO events (ts, chain, tx_hash, log_index, block, kind, token,"
            " amount_native, gas_usd, address, source)"
            " VALUES (1, 'base', ?, -1, 1, 'gas_fee', 'ETH', '0.001', 1.0, ?, 'vaultco')",
            (TX_1, MAIN),
        )
        ledger.execute(
            "INSERT INTO snapshots (ts, chain, address, kind, token, source,"
            " amount_native, usd_at_time)"
            " VALUES (?, 'base', ?, 'position', 'USDC', 'vaultco', '50', 50.0)",
            (int(time.time()), MAIN),
        )
    tag_events(
        ledger,
        {
            (TX_1, "transfer_out"): ["deposit", "swap"],  # real deposits are swap-tagged too
            (TX_1, "transfer_in"): ["deposit", "swap"],
            (TX_2, "transfer_in"): ["withdraw", "swap"],
        },
    )


def test_vault_strategy_profit_counts_each_flow_once(ledger):
    seed_vault_strategy(ledger)
    (row,) = strategy_summary(ledger)
    assert row.source == "vaultco"
    assert row.deposited_usd == 100  # the share mint mirror leg never double-counts
    assert row.withdrawn_usd == 60
    assert row.income_usd == 10
    assert row.gas_usd == 1
    assert row.position_usd == 50
    assert row.profit_usd == 19
    assert row.unpriced_count == 0  # swap-skipped legs don't count as unpriced


def test_venft_purchases_and_rebases_fold_into_voting(ledger):
    # payment to a marketplace: no source, only the manual venft-purchase tag
    ingest_transfers(
        ledger,
        [make_transfer(tx_hash=TX_4, direction="out", counterparty=OUTSIDER, amount=Decimal(20))],
        tracked_addresses=set(),
        price_fn=usd(1),
    )
    tag_events(ledger, {(TX_4, "transfer_out"): ["venft-purchase"]})
    with ledger:
        ledger.execute(
            "INSERT INTO snapshots (ts, chain, address, kind, token, source,"
            " amount_native, usd_at_time)"
            " VALUES (?, 'base', ?, 'claimable', 'AERO', 'aerodrome-rebase', '2', 4.0)",
            (int(time.time()), MAIN),
        )
    (row,) = strategy_summary(ledger)
    assert row.source == "aerodrome-voting"
    assert row.deposited_usd == 20
    assert row.position_usd == 4
    assert row.profit_usd == -16


def test_untagged_and_own_transfers_stay_out(ledger):
    ingest_transfers(
        ledger,
        [make_transfer(amount=Decimal(500))],  # no source: not a strategy
        tracked_addresses=set(),
        price_fn=usd(1),
    )
    assert strategy_summary(ledger) == []
