"""Turn provider transfers/fees into ledger events, idempotently.

Dedup relies on the events UNIQUE(tx_hash, log_index, kind) constraint
with INSERT OR IGNORE: re-running a sync over an overlapping block
range inserts nothing twice. Transfers between two tracked addresses
are auto-tagged 'own-transfer' (origin 'rule') so reports exclude them
from income/spend.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from hrusha.providers.interface import Transfer, TxFee

CHAIN = "base"
OWN_TRANSFER_TAG = "own-transfer"

# 'ETH' or contract address -> USD price at that time, None when unknown
PriceFn = Callable[[str, int], object]


@dataclass
class IngestStats:
    events_inserted: int = 0
    events_skipped: int = 0  # already present (dedup)
    own_transfers_tagged: int = 0


def ingest_transfers(
    conn: sqlite3.Connection,
    transfers: list[Transfer],
    tracked_addresses: set[str],
    price_fn: PriceFn,
) -> IngestStats:
    stats = IngestStats()
    with conn:
        for transfer in transfers:
            if transfer.token_id is not None:
                price = None  # NFTs have no fungible market price
            else:
                price = price_fn(transfer.contract or transfer.token, transfer.ts)
            usd_at_time = float(transfer.amount * price) if price is not None else None
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO events
                    (ts, chain, tx_hash, log_index, block, kind, token, amount_native,
                     usd_at_time, address, counterparty, contract, token_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    transfer.ts,
                    CHAIN,
                    transfer.tx_hash,
                    transfer.log_index,
                    transfer.block,
                    f"transfer_{transfer.direction}",
                    transfer.token,
                    str(transfer.amount),
                    usd_at_time,
                    transfer.address,
                    transfer.counterparty,
                    transfer.contract,
                    transfer.token_id,
                ),
            )
            if cursor.rowcount == 0:
                stats.events_skipped += 1
                continue
            stats.events_inserted += 1
            if transfer.counterparty in tracked_addresses:
                conn.execute(
                    "INSERT OR IGNORE INTO tags (event_id, tag, origin) VALUES (?, ?, 'rule')",
                    (cursor.lastrowid, OWN_TRANSFER_TAG),
                )
                stats.own_transfers_tagged += 1
    return stats


def ingest_fees(
    conn: sqlite3.Connection,
    fees: list[TxFee],
    ts_by_tx: dict[str, int],
    price_fn: PriceFn,
) -> IngestStats:
    stats = IngestStats()
    with conn:
        for fee in fees:
            ts = ts_by_tx.get(fee.tx_hash, 0)
            price = price_fn("ETH", ts)
            usd_at_time = float(fee.amount_eth * price) if price is not None else None
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO events
                    (ts, chain, tx_hash, log_index, block, kind, token, amount_native,
                     usd_at_time, gas_native, gas_usd, address)
                VALUES (?, ?, ?, -1, ?, 'gas_fee', 'ETH', ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    CHAIN,
                    fee.tx_hash,
                    fee.block,
                    str(fee.amount_eth),
                    usd_at_time,
                    str(fee.amount_eth),
                    usd_at_time,
                    fee.address,
                ),
            )
            if cursor.rowcount == 0:
                stats.events_skipped += 1
            else:
                stats.events_inserted += 1
    return stats
