"""Heal: locate missing txs by balance divergence, repair from chain logs."""

from decimal import Decimal

import pytest

from hrusha.ledger.ingest import ingest_transfers
from hrusha.providers.interface import TxFee
from hrusha.service.heal import RawTransferLog, heal
from tests.conftest import MAIN, OUTSIDER, TOKEN_CONTRACT, TX_2, FakeProvider, make_transfer

ADDRESSES = {"main": MAIN}
DECIMALS = 6
SCALE = 10**DECIMALS
LATEST = 1_000_000
DEPOSIT_BLOCK = 100
GAP_BLOCK = 500


def no_price(token_key, ts):
    return None


class FakeReader:
    """Chain truth derived from a scripted list of RawTransferLog per block."""

    def __init__(self, truth: dict[int, list[RawTransferLog]], nft: bool = False):
        self._truth = truth  # block -> logs (ALL logs of the token contract)
        self._nft = nft
        self.balance_calls = 0

    def latest_block(self):
        return LATEST

    def raw_balance(self, contract, address, block):
        self.balance_calls += 1
        total = 0
        for b, logs in self._truth.items():
            if b > block:
                continue
            for entry in logs:
                amount = 1 if self._nft else entry.raw_amount
                if entry.recipient == address:
                    total += amount
                if entry.sender == address:
                    total -= amount
        return total

    def decimals(self, contract):
        return DECIMALS

    def transfer_logs(self, contract, block):
        return self._truth.get(block, [])

    def block_ts(self, block):
        return 1_700_000_000 + block

    def logs_for_block(self, block):  # test helper
        return self._truth.get(block, [])


def erc20_log(sender, recipient, amount, tx_hash=TX_2, log_index=37):
    return RawTransferLog(
        tx_hash=tx_hash,
        log_index=log_index,
        sender=sender,
        recipient=recipient,
        raw_amount=int(amount * SCALE),
        token_id=None,
    )


def seed_deposit(ledger, amount="10"):
    """Ledger knows one 10-token deposit at DEPOSIT_BLOCK."""
    ingest_transfers(
        ledger,
        [make_transfer(block=DEPOSIT_BLOCK, amount=Decimal(amount))],
        tracked_addresses=set(),
        price_fn=no_price,
    )
    # heal only chases tokens the operator has sent; give it one out leg it knows
    ingest_transfers(
        ledger,
        [make_transfer(log_index=8, block=DEPOSIT_BLOCK, direction="out", amount=Decimal("0"))],
        tracked_addresses=set(),
        price_fn=no_price,
    )


def truth_with_gap():
    """Chain truth: the known deposit, plus a withdrawal the ledger missed."""
    return {
        DEPOSIT_BLOCK: [erc20_log(OUTSIDER, MAIN, 10, tx_hash="0x" + "4" * 64, log_index=7)],
        GAP_BLOCK: [erc20_log(MAIN, OUTSIDER, 6)],
    }


def test_heal_finds_and_ingests_the_missing_leg(ledger):
    seed_deposit(ledger)
    reader = FakeReader(truth_with_gap())
    stats = heal(ledger, ADDRESSES, reader, _prices(ledger), FakeProvider(transfers=[]))

    assert stats.gaps_healed == 1
    assert stats.transfers.events_inserted == 1
    assert stats.unexplained == []
    block, kind, amount, ts, log_index = ledger.execute(
        "SELECT block, kind, amount_native, ts, log_index FROM events WHERE tx_hash = ?",
        (TX_2,),
    ).fetchone()
    assert (block, kind, amount) == (GAP_BLOCK, "transfer_out", "6")
    assert ts == 1_700_000_000 + GAP_BLOCK
    assert log_index == 37  # the real chain log index, collision-free


def test_heal_ingests_gas_for_missing_outgoing_tx(ledger):
    seed_deposit(ledger)
    provider = FakeProvider(
        transfers=[],
        fees=[TxFee(tx_hash=TX_2, block=GAP_BLOCK, address=MAIN, amount_eth=Decimal("0.0001"))],
    )
    stats = heal(ledger, ADDRESSES, FakeReader(truth_with_gap()), _prices(ledger), provider)
    assert stats.fees.events_inserted == 1
    fee_ts = ledger.execute(
        "SELECT ts FROM events WHERE kind = 'gas_fee' AND tx_hash = ?", (TX_2,)
    ).fetchone()[0]
    assert fee_ts == 1_700_000_000 + GAP_BLOCK


def test_heal_is_idempotent_once_balances_match(ledger):
    seed_deposit(ledger)
    reader = FakeReader(truth_with_gap())
    prices = _prices(ledger)
    heal(ledger, ADDRESSES, reader, prices, FakeProvider(transfers=[]))
    again = heal(ledger, ADDRESSES, reader, prices, FakeProvider(transfers=[]))
    assert again.gaps_healed == 0
    assert again.transfers.events_inserted == 0
    assert ledger.execute("SELECT COUNT(*) FROM events WHERE kind != 'gas_fee'").fetchone()[0] == 3


def test_matching_token_is_left_alone_without_log_reads(ledger):
    seed_deposit(ledger)
    truth = {DEPOSIT_BLOCK: truth_with_gap()[DEPOSIT_BLOCK]}  # no gap on chain
    reader = FakeReader(truth)
    stats = heal(ledger, ADDRESSES, reader, _prices(ledger), FakeProvider(transfers=[]))
    assert stats.gaps_healed == 0
    assert stats.transfers.events_inserted == 0


def test_balance_move_without_logs_is_reported_unexplained(ledger):
    seed_deposit(ledger)

    class LyingReader(FakeReader):
        def transfer_logs(self, contract, block):
            return []  # the node "loses" the logs: nothing to repair from

    reader = LyingReader(truth_with_gap())
    stats = heal(ledger, ADDRESSES, reader, _prices(ledger), FakeProvider(transfers=[]))
    assert stats.gaps_healed == 0
    assert len(stats.unexplained) == 1
    assert "beyond its Transfer logs" in stats.unexplained[0]


def test_incoming_only_spam_tokens_are_never_chased(ledger):
    # incoming airdrop, never sent: not a candidate no matter what the chain says
    ingest_transfers(
        ledger,
        [make_transfer(amount=Decimal("500"))],
        tracked_addresses=set(),
        price_fn=no_price,
    )
    reader = FakeReader({})
    stats = heal(ledger, ADDRESSES, reader, _prices(ledger), FakeProvider(transfers=[]))
    assert stats.tokens_checked == 0
    assert reader.balance_calls == 0


def test_gaps_beyond_the_sync_cursor_are_left_for_sync(ledger):
    seed_deposit(ledger)
    with ledger:
        ledger.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?)",
            (f"transfers_cursor:{MAIN}", str(GAP_BLOCK)),  # cursor BEFORE the gap
        )
    reader = FakeReader(truth_with_gap())
    stats = heal(ledger, ADDRESSES, reader, _prices(ledger), FakeProvider(transfers=[]))
    assert stats.gaps_healed == 0
    assert stats.transfers.events_inserted == 0
    assert stats.unexplained == []  # not an anomaly: sync just hasn't run yet


def test_partially_indexed_tx_heals_only_the_missing_leg(ledger):
    # Blockscout indexed ONE leg of the gap tx (synthetic ordinal 0);
    # the other leg is missing. Content-dedup must not re-insert the
    # known leg under its real log index.
    seed_deposit(ledger)
    ingest_transfers(
        ledger,
        [
            make_transfer(
                tx_hash=TX_2,
                log_index=0,
                block=GAP_BLOCK,
                direction="out",
                amount=Decimal("6"),
            )
        ],
        tracked_addresses=set(),
        price_fn=no_price,
    )
    truth = truth_with_gap()
    truth[GAP_BLOCK].append(erc20_log(MAIN, OUTSIDER, 2, log_index=38))  # the missing leg
    stats = heal(ledger, ADDRESSES, FakeReader(truth), _prices(ledger), FakeProvider(transfers=[]))

    assert stats.gaps_healed == 1
    assert stats.transfers.events_inserted == 1  # only the 2-token leg
    assert stats.unexplained == []
    legs = ledger.execute(
        "SELECT amount_native, log_index FROM events WHERE tx_hash = ? AND kind != 'gas_fee'"
        " ORDER BY log_index",
        (TX_2,),
    ).fetchall()
    assert legs == [("6", 0), ("2", 38)]  # no duplicate of the known 6-token leg


def test_hostile_decimals_are_refused():
    # raw eth_call is not ABI-bounded: a spam token can answer decimals()
    # with 2**256-1 to grind Decimal exponentiation
    from hrusha.service.heal import MAX_TOKEN_DECIMALS, W3ChainReader

    class HostileW3:
        class eth:
            @staticmethod
            def call(params, block=None):
                return (2**256 - 1).to_bytes(32, "big")

    reader = W3ChainReader(HostileW3())
    with pytest.raises(ValueError, match=str(MAX_TOKEN_DECIMALS)):
        reader.decimals(TOKEN_CONTRACT)


def test_nft_gap_healed_by_count(ledger):
    ingest_transfers(
        ledger,
        [
            make_transfer(block=DEPOSIT_BLOCK, token="veNFT", token_id="70075", amount=Decimal(1)),
            make_transfer(
                log_index=8,
                block=DEPOSIT_BLOCK + 1,
                direction="out",
                token="veNFT",
                token_id="70075",
                amount=Decimal(1),
            ),
            make_transfer(
                log_index=9,
                block=DEPOSIT_BLOCK + 1,
                token="veNFT",
                token_id="75659",
                amount=Decimal(1),
            ),
        ],
        tracked_addresses=set(),
        price_fn=no_price,
    )
    burn = RawTransferLog(
        tx_hash=TX_2,
        log_index=12,
        sender=MAIN,
        recipient="0x" + "0" * 40,
        raw_amount=None,
        token_id="75659",
    )
    truth = {
        DEPOSIT_BLOCK: [
            RawTransferLog(
                tx_hash="0x" + "4" * 64,
                log_index=1,
                sender=OUTSIDER,
                recipient=MAIN,
                raw_amount=None,
                token_id="70075",
            )
        ],
        DEPOSIT_BLOCK + 1: [
            RawTransferLog(
                tx_hash="0x" + "7" * 64,
                log_index=2,
                sender=MAIN,
                recipient=OUTSIDER,
                raw_amount=None,
                token_id="70075",
            ),
            RawTransferLog(
                tx_hash="0x" + "8" * 64,
                log_index=3,
                sender=OUTSIDER,
                recipient=MAIN,
                raw_amount=None,
                token_id="75659",
            ),
        ],
        GAP_BLOCK: [burn],
    }
    stats = heal(
        ledger,
        ADDRESSES,
        FakeReader(truth, nft=True),
        _prices(ledger),
        FakeProvider(transfers=[]),
    )
    assert stats.gaps_healed == 1
    row = ledger.execute(
        "SELECT kind, token_id, amount_native FROM events WHERE tx_hash = ?", (TX_2,)
    ).fetchone()
    assert row == ("transfer_out", "75659", "1")


def _prices(conn):
    import httpx

    from hrusha.prices import PriceResolver

    offline = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    return PriceResolver(conn, FakeProvider(transfers=[]), http=offline)
