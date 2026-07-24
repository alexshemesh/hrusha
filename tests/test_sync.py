from decimal import Decimal
from pathlib import Path

import httpx

from hrusha.config import Config
from hrusha.ledger.store import open_ledger
from hrusha.prices import PriceResolver
from hrusha.service.sync import run_full_sync
from tests.conftest import BLOCK_1, COLD, MAIN, TX_1, FakeProvider, make_fee, make_transfer


def offline_resolver(conn, provider) -> PriceResolver:
    """PriceResolver whose DefiLlama calls fail: prices come from `provider`."""
    offline = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    return PriceResolver(conn, provider, http=offline)


def make_config(tmp_path) -> Config:
    return Config(
        addresses={"main": MAIN, "cold": COLD},
        alchemy_api_key="unused",
        etherscan_api_key=None,
        db_path=Path(tmp_path) / "ledger.db",
    )


def test_full_sync_then_resync_is_idempotent(tmp_path):
    config = make_config(tmp_path)
    provider = FakeProvider(
        transfers=[
            make_transfer(direction="out"),
            make_transfer(log_index=8, address=COLD),
        ],
        fees=[make_fee(tx_hash=TX_1)],
    )
    conn = open_ledger(config.db_path)
    prices = offline_resolver(conn, provider)

    first = run_full_sync(config, provider, conn, prices)
    assert first.transfers.events_inserted == 2
    assert first.fees.events_inserted == 1
    assert first.balance_snapshots == 1

    second = run_full_sync(config, provider, conn, prices)
    assert second.transfers.events_inserted == 0
    assert second.fees.events_inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 3
    conn.close()


def test_cursor_advances_past_last_block(tmp_path):
    config = make_config(tmp_path)
    provider = FakeProvider(transfers=[make_transfer(block=BLOCK_1)])
    conn = open_ledger(config.db_path)

    run_full_sync(config, provider, conn, offline_resolver(conn, provider))
    run_full_sync(config, provider, conn, offline_resolver(conn, provider))

    # first sync starts at 0; second starts one block past the last seen block.
    # Fetches now run concurrently across addresses (Tier 2), so call order is
    # nondeterministic — assert membership + count instead of index.
    assert (MAIN, 0) in provider.transfer_calls
    assert (MAIN, BLOCK_1 + 1) in provider.transfer_calls
    assert len(provider.transfer_calls) == 4  # 2 addresses x 2 syncs
    cursor = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?", (f"transfers_cursor:{MAIN}",)
    ).fetchone()
    assert cursor == (str(BLOCK_1 + 1),)
    conn.close()


def test_transfer_source_overrides_provider_for_transfers(tmp_path):
    config = make_config(tmp_path)
    provider = FakeProvider(transfers=[], fees=[make_fee(tx_hash=TX_1)])
    source = FakeProvider(transfers=[make_transfer(direction="out")])
    conn = open_ledger(config.db_path)

    summary = run_full_sync(
        config, provider, conn, offline_resolver(conn, provider), transfer_source=source
    )

    assert summary.transfers.events_inserted == 1
    assert summary.fees.events_inserted == 1  # receipts still come from `provider`
    assert provider.transfer_calls == []
    assert (MAIN, 0) in source.transfer_calls
    conn.close()


def test_nft_transfers_use_their_own_cursor(tmp_path):
    config = make_config(tmp_path)
    provider = FakeProvider(transfers=[])
    source = FakeProvider(
        transfers=[make_transfer(block=BLOCK_1 + 50)],
        nft_transfers=[make_transfer(log_index=100_000, token_id="76592", block=BLOCK_1)],
    )
    conn = open_ledger(config.db_path)

    run_full_sync(config, provider, conn, offline_resolver(conn, provider), transfer_source=source)
    run_full_sync(config, provider, conn, offline_resolver(conn, provider), transfer_source=source)

    # the NFT cursor starts at 0 and advances independently of the main one,
    # so shipping NFT support after the first backfill still fetches history.
    # Call order is nondeterministic under concurrent fetches — assert membership.
    assert (MAIN, 0) in source.nft_calls
    assert (MAIN, BLOCK_1 + 1) in source.nft_calls
    assert (MAIN, BLOCK_1 + 51) in source.transfer_calls
    assert conn.execute("SELECT COUNT(*) FROM events WHERE token_id IS NOT NULL").fetchone() == (1,)
    conn.close()


def test_snapshots_written(tmp_path):
    config = make_config(tmp_path)
    provider = FakeProvider(transfers=[])
    conn = open_ledger(config.db_path)
    run_full_sync(config, provider, conn, offline_resolver(conn, provider))
    ts, kind, token, amount, usd = conn.execute(
        "SELECT ts, kind, token, amount_native, usd_at_time FROM snapshots"
    ).fetchone()
    assert kind == "balance"
    assert token == "ETH"
    assert Decimal(amount) == Decimal("1.5")
    assert usd == 4500.0
    assert ts > 0
    conn.close()
