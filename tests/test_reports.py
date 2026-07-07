"""strategy_summary: lifetime profit per strategy from ledger flows + snapshots."""

import time
from decimal import Decimal

from hrusha.ledger.ingest import ingest_transfers
from hrusha.ledger.reports import strategy_summary
from hrusha.ledger.tags import set_manual_tag
from tests.conftest import MAIN, OUTSIDER, TOKEN_CONTRACT, TX_1, TX_2, make_transfer

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


def cache_price(conn, key, usd, day="2026-07-07"):
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO price_cache (token, day, usd) VALUES (?, ?, ?)",
            (key, day, usd),
        )


def test_vault_yield_decomposes_profit_into_in_kind_and_price_effect(ledger):
    # coins: -100 deposited + 60 withdrawn + 50 held = +10 USDC earned in-kind
    seed_vault_strategy(ledger)
    cache_price(ledger, TOKEN_CONTRACT, 1.0)
    (row,) = strategy_summary(ledger)
    assert row.yield_items == (("USDC", 10.0, 10.0),)
    assert row.yield_usd == 10.0
    # profit(19) + gas(1) - income(10) - yield(10) = 0: flat prices, no drift
    assert row.price_effect_usd == 0.0


def test_yield_stays_undecomposed_without_a_spot_price(ledger):
    # a partial USD sum would silently misattribute the rest to price effect
    seed_vault_strategy(ledger)
    (row,) = strategy_summary(ledger)
    assert row.yield_items == (("USDC", 10.0, None),)
    assert row.yield_usd is None
    assert row.price_effect_usd is None


def test_native_eth_deposits_fold_into_the_weth_family(ledger):
    ingest_transfers(
        ledger,
        [
            make_transfer(tx_hash=TX_3, direction="out", token="ETH",
                          contract=None, amount=Decimal(2)),
            make_transfer(tx_hash=TX_4, log_index=8, token="WETH",
                          contract="0x" + "e" * 40, amount=Decimal(3)),
        ],
        tracked_addresses=set(),
        price_fn=usd(1),
    )
    with ledger:
        ledger.execute("UPDATE events SET source = 'ethvault'")
    tag_events(ledger, {(TX_3, "transfer_out"): ["deposit"], (TX_4, "transfer_in"): ["withdraw"]})
    cache_price(ledger, "ETH", 2000.0)
    (row,) = strategy_summary(ledger)
    assert row.yield_items == (("WETH", 1.0, 2000.0),)  # -2 ETH +3 WETH = +1 WETH
    assert row.yield_usd == 2000.0


def test_income_style_strategies_have_no_yield_decomposition(ledger):
    # aerodrome locks/purchases are not deposit/withdraw legs
    ingest_transfers(
        ledger,
        [make_transfer(tx_hash=TX_3, direction="out", token="AERO", amount=Decimal(5))],
        tracked_addresses=set(),
        price_fn=usd(1),
    )
    with ledger:
        ledger.execute("UPDATE events SET source = 'aerodrome-voting'")
    tag_events(ledger, {(TX_3, "transfer_out"): ["lock"]})
    (row,) = strategy_summary(ledger)
    assert row.yield_items == ()
    assert row.yield_usd is None
    assert row.price_effect_usd is None
