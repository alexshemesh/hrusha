"""Repair ledger gaps that doctor found, from raw chain data.

Blockscout's Base index can miss entire transactions (proven live: a
vault withdrawal absent from both its account API and its own getLogs,
while the raw RPC receipt shows it plainly). Doctor detects the damage
as a balance mismatch; heal locates and repairs it:

1. For each mismatched token the operator has actually used (at least
   one outgoing leg — spam airdrops never qualify), replay the ledger's
   legs as a step function of expected balance over blocks.
2. Binary-search archive `balanceOf` for the first block where the
   chain departs from the expectation. The missing transaction is in
   that block.
3. Read that block's Transfer logs straight from the RPC node (not the
   indexer that failed us), ingest the missing legs — priced, deduped,
   and fee-accounted like any synced transfer — and continue until the
   ledger explains the chain up to the sync cursor. Beyond the cursor
   is sync's territory: healing there would duplicate legs when sync
   later fetches the same tx under a synthetic ordinal.

Everything is additive (INSERT OR IGNORE); heal never deletes or edits.
A balance change with no Transfer logs in its block is reported as
unexplained, not guessed at. Limitation: two missing transfers that
exactly cancel within one probe interval are invisible to balance
probes — doctor and heal converge on "ledger explains the chain", which
is the strongest claim balance reconciliation can make.

Chain access is one small injected interface so the search logic is
testable without web3 or a network.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from hrusha.adapters.known_contracts import TRANSFER_EVENT_TOPIC
from hrusha.ledger.ingest import IngestStats, ingest_fees, ingest_transfers
from hrusha.prices import PriceResolver
from hrusha.providers.interface import DataProvider, Transfer

log = logging.getLogger("hrusha.heal")

CHAIN = "base"
MAX_GAP_ROUNDS = 12  # missing txs healed per token before giving up
MAX_TOKEN_DECIMALS = 77  # ~10**77 covers uint256; anything above is hostile


@dataclass(frozen=True)
class RawTransferLog:
    """One Transfer event as read from an RPC receipt/log."""

    tx_hash: str
    log_index: int  # the real chain log index — globally unique within a tx
    sender: str
    recipient: str
    raw_amount: int | None  # ERC-20 amount in base units; None for ERC-721
    token_id: str | None  # ERC-721 id; None for ERC-20


class ChainReader(Protocol):
    """The few chain reads heal needs; web3-backed in production."""

    def latest_block(self) -> int: ...

    def raw_balance(self, contract: str, address: str, block: int) -> int:
        """balanceOf at end of `block` (archive read), in base units."""
        ...

    def decimals(self, contract: str) -> int: ...

    def transfer_logs(self, contract: str, block: int) -> list[RawTransferLog]:
        """All Transfer events of `contract` inside `block`, from the node."""
        ...

    def block_ts(self, block: int) -> int: ...


@dataclass
class HealStats:
    tokens_checked: int = 0
    gaps_healed: int = 0  # divergence blocks explained and repaired
    transfers: IngestStats = field(default_factory=IngestStats)
    fees: IngestStats = field(default_factory=IngestStats)
    unexplained: list[str] = field(default_factory=list)  # human-readable notes


def heal(
    conn: sqlite3.Connection,
    addresses: dict[str, str],
    reader: ChainReader,
    prices: PriceResolver,
    provider: DataProvider,
) -> HealStats:
    stats = HealStats()
    tracked = set(addresses.values())
    latest = reader.latest_block()
    for address in addresses.values():
        for contract in _used_contracts(conn, address):
            stats.tokens_checked += 1
            # never probe past the sync cursor: everything beyond it is
            # sync's territory, and healing it would duplicate legs when
            # sync later fetches the same tx under a synthetic ordinal
            horizon = min(latest, _sync_horizon(conn, address, contract, latest))
            try:
                _heal_token(
                    conn, reader, prices, provider, stats, address, contract, horizon, tracked
                )
            except Exception as exc:
                # spam tokens fabricate out-legs and then revert on balanceOf/
                # decimals; one bad contract must not abort the whole repair
                log.warning(
                    "token heal failed",
                    extra={"contract": contract, "error": exc.__class__.__name__},
                )
                stats.unexplained.append(
                    f"{_symbol(conn, address, contract)} ({contract[:10]}…): chain read failed "
                    f"({exc.__class__.__name__})"
                )
    return stats


def _sync_horizon(conn: sqlite3.Connection, address: str, contract: str, latest: int) -> int:
    """Last block heal may repair: one before the sync cursor of this token's
    family (fungible vs NFT cursors advance independently). A ledger without
    a cursor was never synced — its legs are synthetic (tests) or orphaned,
    and there is no re-fetch to collide with, so the live head is fine."""
    key = "nft_cursor" if _is_nft(conn, address, contract) else "transfers_cursor"
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?", (f"{key}:{address}",)
    ).fetchone()
    return int(row[0]) - 1 if row else latest


def _used_contracts(conn: sqlite3.Connection, address: str) -> list[str]:
    """Contracts the operator has actually sent: gaps there are worth chasing;
    spam airdrops (incoming only, balances self-manipulated) are not."""
    return [
        contract
        for (contract,) in conn.execute(
            """
            SELECT DISTINCT contract FROM events
            WHERE address = ? AND contract IS NOT NULL AND kind = 'transfer_out'
            ORDER BY contract
            """,
            (address,),
        ).fetchall()
    ]


def _heal_token(
    conn: sqlite3.Connection,
    reader: ChainReader,
    prices: PriceResolver,
    provider: DataProvider,
    stats: HealStats,
    address: str,
    contract: str,
    horizon: int,
    tracked: set[str],
) -> None:
    is_nft = _is_nft(conn, address, contract)
    symbol = _symbol(conn, address, contract)
    # spam tokens mimic real symbols (fake "USDC"), so notes carry the contract
    name = f"{symbol} ({contract[:10]}…)"
    scale = Decimal(1) if is_nft else Decimal(10) ** reader.decimals(contract)

    def balance_at(block: int) -> Decimal:
        return Decimal(reader.raw_balance(contract, address, block)) / scale

    steps = _ledger_steps(conn, address, contract, is_nft)

    def expected(block: int) -> Decimal:
        return sum((delta for b, delta in steps if b <= block), Decimal(0))

    if balance_at(horizon) == expected(horizon):
        return  # ledger explains the chain up to the sync cursor

    first_block = steps[0][0] if steps else horizon
    known_good = first_block - 1
    if balance_at(known_good) != 0:
        stats.unexplained.append(
            f"{name}: balance predates the first ledger leg (block {first_block})"
        )
        return

    for _ in range(MAX_GAP_ROUNDS):
        if balance_at(horizon) == expected(horizon):
            return
        gap_block = _first_divergence(balance_at, expected, known_good, horizon)
        healed = _ingest_block_legs(
            conn, reader, prices, provider, stats, address, contract, symbol, gap_block, tracked
        )
        for leg in healed:
            delta = leg.amount if leg.direction == "in" else -leg.amount
            steps.append((gap_block, delta))
        if not healed or balance_at(gap_block) != expected(gap_block):
            stats.unexplained.append(
                f"{name}: balance moved at block {gap_block} beyond its Transfer logs"
            )
            return
        stats.gaps_healed += 1
        known_good = gap_block
    stats.unexplained.append(f"{name}: still diverging after {MAX_GAP_ROUNDS} healed gaps")


def _first_divergence(balance_at, expected, known_good: int, latest: int) -> int:
    """Smallest block in (known_good, latest] where chain != expectation."""
    lo, hi = known_good, latest
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if balance_at(mid) == expected(mid):
            lo = mid
        else:
            hi = mid
    return hi


def _ingest_block_legs(
    conn: sqlite3.Connection,
    reader: ChainReader,
    prices: PriceResolver,
    provider: DataProvider,
    stats: HealStats,
    address: str,
    contract: str,
    symbol: str,
    block: int,
    tracked: set[str],
) -> list[Transfer]:
    ts = reader.block_ts(block)
    transfers = []
    for raw in reader.transfer_logs(contract, block):
        mine_in = raw.recipient == address
        mine_out = raw.sender == address
        if mine_in == mine_out:
            continue  # not ours, or a self-transfer (net zero)
        if raw.token_id is not None:
            amount = Decimal(1)
        else:
            # decimals() only for fungible legs: ERC-721 contracts revert on it
            amount = Decimal(raw.raw_amount) / Decimal(10) ** reader.decimals(contract)
        transfer = Transfer(
            tx_hash=raw.tx_hash,
            log_index=raw.log_index,
            block=block,
            ts=ts,
            direction="in" if mine_in else "out",
            address=address,
            counterparty=(raw.sender if mine_in else raw.recipient) or None,
            token=symbol,
            contract=contract,
            amount=amount,
            token_id=raw.token_id,
        )
        # partially-indexed txs: Blockscout may have SOME legs of this block
        # already, under synthetic ordinals the UNIQUE constraint can't match
        # against real log indexes — dedup by content, not by log_index
        if not _already_ledgered(conn, transfer):
            transfers.append(transfer)
    if not transfers:
        return []
    log.info(
        "healing missing legs",
        extra={"contract": contract, "block": block, "legs": len(transfers)},
    )
    _merge(stats.transfers, ingest_transfers(conn, transfers, tracked, prices.usd_price))
    # the missing tx's gas is missing too when the operator sent it;
    # provider.tx_fees filters to txs actually sent by `address`
    outgoing = sorted({t.tx_hash for t in transfers if t.direction == "out"})
    if outgoing:
        fees = provider.tx_fees(outgoing, address)
        _merge(
            stats.fees,
            ingest_fees(conn, fees, {t.tx_hash: t.ts for t in transfers}, prices.usd_price),
        )
    return transfers


def _already_ledgered(conn: sqlite3.Connection, transfer: Transfer) -> bool:
    return (
        conn.execute(
            """
            SELECT 1 FROM events
            WHERE tx_hash = ? AND kind = ? AND address = ? AND contract = ?
              AND amount_native = ? AND token_id IS ?
            """,
            (
                transfer.tx_hash,
                f"transfer_{transfer.direction}",
                transfer.address,
                transfer.contract,
                str(transfer.amount),
                transfer.token_id,
            ),
        ).fetchone()
        is not None
    )


def _ledger_steps(
    conn: sqlite3.Connection, address: str, contract: str, is_nft: bool
) -> list[tuple[int, Decimal]]:
    rows = conn.execute(
        """
        SELECT block, kind, amount_native FROM events
        WHERE address = ? AND contract = ? AND kind IN ('transfer_in', 'transfer_out')
          AND (token_id IS NOT NULL) = ? ORDER BY block
        """,
        (address, contract, int(is_nft)),
    ).fetchall()
    return [
        (block, Decimal(amount) if kind == "transfer_in" else -Decimal(amount))
        for block, kind, amount in rows
    ]


def _is_nft(conn: sqlite3.Connection, address: str, contract: str) -> bool:
    row = conn.execute(
        "SELECT MAX(token_id IS NOT NULL) FROM events WHERE address = ? AND contract = ?",
        (address, contract),
    ).fetchone()
    return bool(row and row[0])


def _symbol(conn: sqlite3.Connection, address: str, contract: str) -> str:
    row = conn.execute(
        "SELECT MAX(token) FROM events WHERE address = ? AND contract = ?",
        (address, contract),
    ).fetchone()
    return (row and row[0]) or contract


def _merge(total: IngestStats, part: IngestStats) -> None:
    total.events_inserted += part.events_inserted
    total.events_skipped += part.events_skipped
    total.own_transfers_tagged += part.own_transfers_tagged


# -- web3 wiring ---------------------------------------------------------------


class W3ChainReader:
    """ChainReader over a live web3 connection (needs archive reads, which
    Base RPC providers serve for the depth this project cares about)."""

    def __init__(self, w3) -> None:
        self._w3 = w3
        self._decimals: dict[str, int] = {}

    def latest_block(self) -> int:
        return int(self._w3.eth.block_number)

    def raw_balance(self, contract: str, address: str, block: int) -> int:
        data = "0x70a08231" + "0" * 24 + address[2:].lower()
        raw = self._w3.eth.call({"to": _checksum(contract), "data": data}, block)
        return int.from_bytes(raw, "big")

    def decimals(self, contract: str) -> int:
        if contract not in self._decimals:
            raw = self._w3.eth.call({"to": _checksum(contract), "data": "0x313ce567"})
            value = int.from_bytes(raw, "big")
            if value > MAX_TOKEN_DECIMALS:
                # raw eth_call is not ABI-bounded: a hostile token can answer
                # with a huge exponent to grind Decimal math — refuse early
                raise ValueError(f"token decimals {value} exceed {MAX_TOKEN_DECIMALS}")
            self._decimals[contract] = value
        return self._decimals[contract]

    def transfer_logs(self, contract: str, block: int) -> list[RawTransferLog]:
        entries = self._w3.eth.get_logs(
            {
                "address": _checksum(contract),
                "fromBlock": block,
                "toBlock": block,
                "topics": [TRANSFER_EVENT_TOPIC],
            }
        )
        logs = []
        for entry in entries:
            topics = entry["topics"]
            if len(topics) < 3:
                continue  # not a standard Transfer(from, to, ...) event
            is_nft = len(topics) == 4  # ERC-721 indexes tokenId as the 4th topic
            logs.append(
                RawTransferLog(
                    tx_hash="0x" + bytes(entry["transactionHash"]).hex(),
                    log_index=int(entry["logIndex"]),
                    sender=_topic_address(topics[1]),
                    recipient=_topic_address(topics[2]),
                    raw_amount=None if is_nft else int.from_bytes(entry["data"], "big"),
                    token_id=str(int.from_bytes(bytes(topics[3]), "big")) if is_nft else None,
                )
            )
        return logs

    def block_ts(self, block: int) -> int:
        return int(self._w3.eth.get_block(block)["timestamp"])


def _checksum(address: str):
    from web3 import Web3  # deferred: web3 import is slow

    return Web3.to_checksum_address(address)


def _topic_address(topic) -> str:
    return "0x" + bytes(topic).hex()[-40:]
