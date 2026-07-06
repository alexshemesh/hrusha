"""DataProvider: the seam between hrusha and whoever supplies chain data.

Everything downstream (ledger ingestion, reports, dashboard) consumes
these types only. Swapping Alchemy for DeBank later (docs/DESIGN.md)
means writing one new class implementing this protocol; nothing else
changes.

Amounts are Decimal end to end — token amounts exceed float precision.
Addresses are lowercase 0x hex throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


class ProviderError(Exception):
    """An upstream data-provider call failed. Messages never contain API keys."""


@dataclass(frozen=True)
class TokenBalance:
    address: str  # wallet holding the balance
    token: str  # symbol; 'ETH' for the native coin
    contract: str | None  # token contract; None for the native coin
    amount: Decimal
    usd_price: Decimal | None
    usd_value: Decimal | None


@dataclass(frozen=True)
class Transfer:
    tx_hash: str
    log_index: int  # -1 for top-level (external) transfers
    block: int
    ts: int  # unix seconds, block timestamp
    direction: str  # 'in' | 'out' relative to `address`
    address: str  # the tracked wallet this transfer belongs to
    counterparty: str | None
    token: str  # symbol; 'ETH' for the native coin
    contract: str | None
    amount: Decimal
    token_id: str | None = None  # ERC-721 id; None for fungible transfers


@dataclass(frozen=True)
class TxFee:
    tx_hash: str
    block: int
    address: str  # tx sender (who paid the fee)
    amount_eth: Decimal  # execution fee + L1 data fee (OP-stack chains)


class TransferSource(Protocol):
    """Just transfer history — the one surface worth sourcing separately.

    Alchemy load-sheds its Transfers API for free-tier apps while serving
    everything else fine, so sync takes the transfer source as its own
    dependency (Blockscout in v1) next to the main DataProvider.
    """

    def transfers(self, address: str, since_block: int) -> list[Transfer]:
        """All in/out transfers touching `address` from `since_block`, oldest first."""
        ...

    def nft_transfers(self, address: str, since_block: int) -> list[Transfer]:
        """ERC-721 transfers (token_id set, amount 1), oldest first.

        Sync keeps a separate cursor for these so adding NFT support
        backfills existing ledgers without resetting the main cursor.
        """
        ...


class DataProvider(Protocol):
    """Chain-data source for one network (Base in v1)."""

    def balances(self, addresses: dict[str, str]) -> list[TokenBalance]:
        """Current token balances with USD prices for label -> address map."""
        ...

    def transfers(self, address: str, since_block: int) -> list[Transfer]:
        """All in/out transfers touching `address` from `since_block`, oldest first."""
        ...

    def tx_fees(self, tx_hashes: list[str], address: str) -> list[TxFee]:
        """Fees for the given txs, restricted to those actually sent by `address`."""
        ...

    def historical_usd_price(self, token_key: str, ts: int) -> Decimal | None:
        """USD price of 'ETH' or a token contract address around unix time `ts`.

        Returns None when the source definitively has no data for the token;
        raises ProviderError when the lookup itself failed (retry later).
        """
        ...

    def positions(self, address: str) -> list:
        """DeFi positions. Protocol adapters (Phase 3/4) or DeBank provide this."""
        ...

    def claimables(self, address: str) -> list:
        """Pending rewards. Protocol adapters (Phase 3/4) or DeBank provide this."""
        ...
