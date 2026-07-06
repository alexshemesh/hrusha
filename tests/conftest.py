"""Shared synthetic fixtures.

Addresses, contracts, and tx hashes follow the synthetic convention
enforced by .gitleaks.toml: 40+ identical leading hex chars after 0x.
Never put real chain data in tests — the repo is public.
"""

from decimal import Decimal

import pytest

from hrusha.providers.interface import TokenBalance, Transfer, TxFee

MAIN = "0x" + "1" * 40
COLD = "0x" + "2" * 40
OUTSIDER = "0x" + "3" * 40
TOKEN_CONTRACT = "0x" + "a" * 40
TX_1 = "0x" + "4" * 64
TX_2 = "0x" + "5" * 64
TX_OWN = "0x" + "6" * 64

TS_1 = 1_750_000_000
BLOCK_1 = 31_000_000


def make_transfer(**overrides) -> Transfer:
    values = dict(
        tx_hash=TX_1,
        log_index=7,
        block=BLOCK_1,
        ts=TS_1,
        direction="in",
        address=MAIN,
        counterparty=OUTSIDER,
        token="USDC",
        contract=TOKEN_CONTRACT,
        amount=Decimal("100.5"),
    )
    values.update(overrides)
    return Transfer(**values)


def make_fee(**overrides) -> TxFee:
    values = dict(tx_hash=TX_1, block=BLOCK_1, address=MAIN, amount_eth=Decimal("0.00021"))
    values.update(overrides)
    return TxFee(**values)


class FakeProvider:
    """In-memory DataProvider for sync/CLI tests."""

    def __init__(self, api_key: str = "unused", transfers=None, fees=None, balances=None):
        self._transfers = transfers if transfers is not None else [make_transfer()]
        self._fees = fees if fees is not None else []
        self._balances = (
            balances
            if balances is not None
            else [
                TokenBalance(
                    address=MAIN,
                    token="ETH",
                    contract=None,
                    amount=Decimal("1.5"),
                    usd_price=Decimal("3000"),
                    usd_value=Decimal("4500"),
                )
            ]
        )
        self.transfer_calls: list[tuple[str, int]] = []

    def balances(self, addresses):
        return self._balances

    def transfers(self, address, since_block):
        self.transfer_calls.append((address, since_block))
        return [t for t in self._transfers if t.address == address and t.block >= since_block]

    def tx_fees(self, tx_hashes, address):
        wanted = set(tx_hashes)
        return [f for f in self._fees if f.tx_hash in wanted and f.address == address]

    def historical_usd_price(self, token_key, ts):
        return Decimal("2.0")

    def positions(self, address):
        raise NotImplementedError

    def claimables(self, address):
        raise NotImplementedError


@pytest.fixture
def ledger(tmp_path):
    from hrusha.ledger.store import open_ledger

    conn = open_ledger(tmp_path / "ledger.db")
    yield conn
    conn.close()
