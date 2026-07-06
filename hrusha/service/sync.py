"""Full sync: chain -> ledger, idempotent and resumable.

Per tracked address: read the block cursor from sync_state, fetch
transfers since it, fetch receipts for outgoing txs (fee accounting),
ingest everything (dedup makes overlaps harmless), advance the cursor.
Finally snapshot current balances. A crash mid-run loses nothing: the
cursor only advances after its address's events are committed.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field

from hrusha.adapters.known_contracts import seed_default_rules
from hrusha.config import Config
from hrusha.ledger.ingest import IngestStats, ingest_fees, ingest_transfers
from hrusha.ledger.tags import retag_all
from hrusha.prices import PriceResolver
from hrusha.providers.interface import DataProvider, TransferSource

CHAIN = "base"
CURSOR_KEY_TEMPLATE = "transfers_cursor:{address}"

log = logging.getLogger("hrusha.sync")


@dataclass
class SyncSummary:
    sync_run_id: str
    transfers: IngestStats = field(default_factory=IngestStats)
    fees: IngestStats = field(default_factory=IngestStats)
    balance_snapshots: int = 0


def run_full_sync(
    config: Config,
    provider: DataProvider,
    conn: sqlite3.Connection,
    prices: PriceResolver,
    transfer_source: TransferSource | None = None,
) -> SyncSummary:
    """Sync the ledger. Transfers come from `transfer_source` (defaults to
    `provider`); balances, receipts and prices always come from `provider`."""
    summary = SyncSummary(sync_run_id=uuid.uuid4().hex[:12])
    transfer_source = transfer_source or provider
    tracked = set(config.addresses.values())
    for label, address in config.addresses.items():
        _sync_address(conn, provider, transfer_source, prices, summary, label, address, tracked)
    seed_default_rules(conn)
    tag_stats = retag_all(conn, tracked)
    log.info(
        "tagging finished",
        extra={
            "sync_run_id": summary.sync_run_id,
            "rules_run": tag_stats.rules_run,
            "tags_applied": tag_stats.tags_applied,
            "sources_set": tag_stats.sources_set,
            "epochs_assigned": tag_stats.epochs_assigned,
        },
    )
    summary.balance_snapshots = _snapshot_balances(conn, provider, config)
    log.info(
        "sync finished",
        extra={
            "sync_run_id": summary.sync_run_id,
            "transfers_inserted": summary.transfers.events_inserted,
            "fees_inserted": summary.fees.events_inserted,
            "duplicates_skipped": summary.transfers.events_skipped + summary.fees.events_skipped,
            "balance_snapshots": summary.balance_snapshots,
        },
    )
    return summary


def _sync_address(
    conn: sqlite3.Connection,
    provider: DataProvider,
    transfer_source: TransferSource,
    prices: PriceResolver,
    summary: SyncSummary,
    label: str,
    address: str,
    tracked: set[str],
) -> None:
    since_block = _cursor(conn, address)
    log.info(
        "syncing address",
        extra={
            "sync_run_id": summary.sync_run_id,
            "label": label,
            "since_block": since_block,
            "transfer_source": type(transfer_source).__name__,
        },
    )
    transfers = transfer_source.transfers(address, since_block=since_block)
    if not transfers:
        return

    outgoing_hashes = [t.tx_hash for t in transfers if t.direction == "out"]
    fees = provider.tx_fees(outgoing_hashes, address)
    ts_by_tx = {t.tx_hash: t.ts for t in transfers}

    transfer_stats = ingest_transfers(conn, transfers, tracked, prices.usd_price)
    fee_stats = ingest_fees(conn, fees, ts_by_tx, prices.usd_price)
    _merge(summary.transfers, transfer_stats)
    _merge(summary.fees, fee_stats)

    _set_cursor(conn, address, max(t.block for t in transfers) + 1)


def _snapshot_balances(conn: sqlite3.Connection, provider: DataProvider, config: Config) -> int:
    now = int(time.time())
    balances = provider.balances(config.addresses)
    with conn:
        for b in balances:
            conn.execute(
                """
                INSERT INTO snapshots (ts, chain, address, kind, token, amount_native,
                                       usd_at_time)
                VALUES (?, ?, ?, 'balance', ?, ?, ?)
                """,
                (
                    now,
                    CHAIN,
                    b.address,
                    b.token,
                    str(b.amount),
                    float(b.usd_value) if b.usd_value is not None else None,
                ),
            )
    return len(balances)


def _cursor(conn: sqlite3.Connection, address: str) -> int:
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?",
        (CURSOR_KEY_TEMPLATE.format(address=address),),
    ).fetchone()
    return int(row[0]) if row else 0


def _set_cursor(conn: sqlite3.Connection, address: str, block: int) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            (CURSOR_KEY_TEMPLATE.format(address=address), str(block)),
        )


def _merge(total: IngestStats, part: IngestStats) -> None:
    total.events_inserted += part.events_inserted
    total.events_skipped += part.events_skipped
    total.own_transfers_tagged += part.own_transfers_tagged
