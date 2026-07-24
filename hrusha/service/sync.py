"""Full sync: chain -> ledger, idempotent and resumable.

Per tracked address: read the block cursor from sync_state, fetch
transfers since it, fetch receipts for outgoing txs (fee accounting),
ingest everything (dedup makes overlaps harmless), advance the cursor.
Finally snapshot current balances. A crash mid-run loses nothing: the
cursor only advances after its address's events are committed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from hrusha.adapters.aerodrome import AerodromeAdapter, discover_claim_rules
from hrusha.adapters.forty_acres import FortyAcresAdapter
from hrusha.adapters.known_contracts import (
    AERO_CONTRACT,
    SOURCE_40ACRES,
    SOURCE_AERODROME,
    SOURCE_AERODROME_REBASE,
    SOURCE_MORPHO,
    seed_default_rules,
)
from hrusha.adapters.morpho import MorphoAdapter, discover_vault_rules
from hrusha.config import Config
from hrusha.ledger.ingest import IngestStats, ingest_fees, ingest_transfers
from hrusha.ledger.tags import retag_all
from hrusha.prices import PriceResolver
from hrusha.providers.interface import DataProvider, TransferSource

CHAIN = "base"
CURSOR_KEY_TEMPLATE = "transfers_cursor:{address}"
# NFTs cursor separately: the feature shipped after the first backfills, so
# a shared cursor would silently skip all historical veNFT trades
NFT_CURSOR_KEY_TEMPLATE = "nft_cursor:{address}"
AERO_VOTE_POOLS_KEY_TEMPLATE = "aero_vote_pools:{venft_id}"
# Cap concurrent address fetches. Alchemy rate-lits at ~660 CU/s/cu-sec;
# transfer + receipt batches are cheap but we stay conservative to avoid
# 429 storms. Writes stay single-threaded regardless (see run_full_sync).
MAX_FETCH_WORKERS = 4

log = logging.getLogger("hrusha.sync")


@dataclass
class SyncSummary:
    sync_run_id: str
    transfers: IngestStats = field(default_factory=IngestStats)
    fees: IngestStats = field(default_factory=IngestStats)
    balance_snapshots: int = 0
    aerodrome_snapshots: int = 0  # veNFT positions + claimables
    morpho_snapshots: int = 0  # active vault positions
    forty_acres_snapshots: int = 0  # active supply positions


@dataclass
class _AddressFetch:
    """Network-only result for one address; ingested serially on main thread."""

    label: str
    address: str
    transfers: list = field(default_factory=list)
    fees: list = field(default_factory=list)
    nft_transfers: list = field(default_factory=list)
    nft_fees: list = field(default_factory=list)


def run_full_sync(
    config: Config,
    provider: DataProvider,
    conn: sqlite3.Connection,
    prices: PriceResolver,
    transfer_source: TransferSource | None = None,
    aerodrome: AerodromeAdapter | None = None,
    morpho: MorphoAdapter | None = None,
    forty_acres: FortyAcresAdapter | None = None,
) -> SyncSummary:
    """Sync the ledger. Transfers come from `transfer_source` (defaults to
    `provider`); balances, receipts and prices always come from `provider`;
    the optional Aerodrome adapter contributes claim rules and position/
    claimable snapshots."""
    summary = SyncSummary(sync_run_id=uuid.uuid4().hex[:12])
    transfer_source = transfer_source or provider
    tracked = set(config.addresses.values())
    has_nft = hasattr(transfer_source, "nft_transfers")

    # --- Phase 1 (serial, main thread): read per-address block cursors.
    # Cheap; needs the SQLite connection which must not cross threads.
    fetch_jobs: list[tuple[str, str, int, int | None]] = []
    for label, address in config.addresses.items():
        since_block = _cursor(conn, address)
        nft_since = _cursor(conn, address, NFT_CURSOR_KEY_TEMPLATE) if has_nft else None
        log.info(
            "syncing address",
            extra={
                "sync_run_id": summary.sync_run_id,
                "label": label,
                "since_block": since_block,
                "transfer_source": type(transfer_source).__name__,
            },
        )
        fetch_jobs.append((label, address, since_block, nft_since))

    # --- Phase 2 (parallel): network fetches only. No SQLite, no prices in
    # worker threads — workers return dataclass results. httpx.Client is
    # thread-safe for concurrent requests; per-address cursors are
    # independent. A failed fetch propagates (fail-fast) and aborts before
    # any ingest, matching the previous serial behaviour.
    workers = min(MAX_FETCH_WORKERS, max(1, len(fetch_jobs)))
    fetches: dict[str, _AddressFetch] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(_fetch_address, transfer_source, provider, label, address, sb, nsb): address
            for label, address, sb, nsb in fetch_jobs
        }
        for fut in futs:
            fetch = fut.result()  # raises on fetch error -> fail-fast
            fetches[fetch.address] = fetch

    # --- Phase 3 (serial, main thread): ingest + advance cursors in
    # address order. Prices still resolved inline here (B3 defers that).
    for _label, address, _sb, _nsb in fetch_jobs:
        _ingest_address(conn, prices, summary, fetches[address], tracked)
    seed_default_rules(conn)
    if aerodrome is not None:
        rules_added = discover_claim_rules(conn, aerodrome)
        log.info(
            "aerodrome claim rules discovered",
            extra={"sync_run_id": summary.sync_run_id, "rules_added": rules_added},
        )
    if morpho is not None:
        vault_rules = discover_vault_rules(conn, morpho, list(config.addresses.values()))
        log.info(
            "morpho vault rules discovered",
            extra={"sync_run_id": summary.sync_run_id, "rules_added": vault_rules},
        )
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
    if aerodrome is not None:
        summary.aerodrome_snapshots = _snapshot_aerodrome(conn, aerodrome, config, prices)
    if morpho is not None:
        summary.morpho_snapshots = _snapshot_morpho(conn, morpho, config)
    if forty_acres is not None:
        summary.forty_acres_snapshots = _snapshot_forty_acres(conn, forty_acres, config, prices)
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


def _fetch_address(
    transfer_source: TransferSource,
    provider: DataProvider,
    label: str,
    address: str,
    since_block: int,
    nft_since_block: int | None,
) -> _AddressFetch:
    """Pure-network fetch for one address. No SQLite, no prices — safe to
    run in a worker thread. httpx.Client is thread-safe for concurrent
    requests."""
    result = _AddressFetch(label=label, address=address)
    transfers = transfer_source.transfers(address, since_block=since_block)
    if transfers:
        result.transfers = transfers
        outgoing = [t.tx_hash for t in transfers if t.direction == "out"]
        result.fees = provider.tx_fees(outgoing, address)
    if nft_since_block is not None:
        nft = transfer_source.nft_transfers(address, since_block=nft_since_block)
        if nft:
            result.nft_transfers = nft
            # NFT-only txs (merges, splits) still burn gas; dedup absorbs
            # overlaps with the ERC-20 fee batch above.
            nft_outgoing = [t.tx_hash for t in nft if t.direction == "out"]
            result.nft_fees = provider.tx_fees(nft_outgoing, address)
    return result


def _ingest_address(
    conn: sqlite3.Connection,
    prices: PriceResolver,
    summary: SyncSummary,
    fetch: _AddressFetch,
    tracked: set[str],
) -> None:
    """Serial ingest of one address's fetched data (main thread only).
    All SQLite access stays here; workers never touch the connection."""
    if fetch.transfers:
        ts_by_tx = {t.tx_hash: t.ts for t in fetch.transfers}
        _merge(
            summary.transfers,
            ingest_transfers(conn, fetch.transfers, tracked, prices.usd_price),
        )
        _merge(summary.fees, ingest_fees(conn, fetch.fees, ts_by_tx, prices.usd_price))
        _set_cursor(conn, fetch.address, max(t.block for t in fetch.transfers) + 1)
    if fetch.nft_transfers:
        log.info(
            "nft transfers fetched",
            extra={"sync_run_id": summary.sync_run_id, "count": len(fetch.nft_transfers)},
        )
        ts_by_tx = {t.tx_hash: t.ts for t in fetch.nft_transfers}
        _merge(
            summary.transfers,
            ingest_transfers(conn, fetch.nft_transfers, tracked, prices.usd_price),
        )
        _merge(summary.fees, ingest_fees(conn, fetch.nft_fees, ts_by_tx, prices.usd_price))
        _set_cursor(
            conn,
            fetch.address,
            max(t.block for t in fetch.nft_transfers) + 1,
            NFT_CURSOR_KEY_TEMPLATE,
        )


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


def _snapshot_aerodrome(
    conn: sqlite3.Connection,
    aerodrome: AerodromeAdapter,
    config: Config,
    prices: PriceResolver,
) -> int:
    """Write veNFT lock positions and pending claimables as snapshots."""
    now = int(time.time())
    count = 0
    with conn:
        for address in config.addresses.values():
            for nft in aerodrome.venfts(address):
                aero_price = prices.usd_price(AERO_CONTRACT, now)
                conn.execute(
                    """
                    INSERT INTO snapshots (ts, chain, address, kind, token, source,
                                           amount_native, usd_at_time)
                    VALUES (?, ?, ?, 'position', 'AERO', ?, ?, ?)
                    """,
                    (
                        now,
                        CHAIN,
                        address,
                        SOURCE_AERODROME,
                        str(nft.locked_aero),
                        float(nft.locked_aero * aero_price) if aero_price is not None else None,
                    ),
                )
                count += 1
                if nft.rebase_aero > 0:
                    # pending rebase: claimable AERO that will compound into
                    # the lock, never through the wallet — snapshot-only
                    conn.execute(
                        """
                        INSERT INTO snapshots (ts, chain, address, kind, token, source,
                                               amount_native, usd_at_time)
                        VALUES (?, ?, ?, 'claimable', 'AERO', ?, ?, ?)
                        """,
                        (
                            now,
                            CHAIN,
                            address,
                            SOURCE_AERODROME_REBASE,
                            str(nft.rebase_aero),
                            float(nft.rebase_aero * aero_price) if aero_price is not None else None,
                        ),
                    )
                    count += 1
                claimable_pools = _aerodrome_claimable_pools(conn, aerodrome, nft)
                for claimable in aerodrome.claimables(nft.id, claimable_pools):
                    price = prices.usd_price(claimable.token, now)
                    conn.execute(
                        """
                        INSERT INTO snapshots (ts, chain, address, kind, token, source,
                                               amount_native, usd_at_time)
                        VALUES (?, ?, ?, 'claimable', ?, ?, ?, ?)
                        """,
                        (
                            now,
                            CHAIN,
                            address,
                            claimable.token,
                            SOURCE_AERODROME,
                            str(claimable.amount),
                            float(claimable.amount * price) if price is not None else None,
                        ),
                    )
                    count += 1
    return count


def _aerodrome_claimable_pools(conn, aerodrome, nft) -> tuple[str, ...]:
    pools_key = AERO_VOTE_POOLS_KEY_TEMPLATE.format(venft_id=nft.id)
    pools_row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (pools_key,)).fetchone()

    pools = set(json.loads(pools_row[0])) if pools_row else set()
    pools.update(pool.lower() for pool, _weight in nft.votes)
    ordered = tuple(sorted(pools))

    conn.execute(
        "INSERT INTO sync_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (pools_key, json.dumps(ordered, separators=(",", ":"))),
    )
    return ordered


def _snapshot_morpho(conn: sqlite3.Connection, morpho: MorphoAdapter, config: Config) -> int:
    """Write active Morpho vault positions (USD valued by Morpho itself)."""
    now = int(time.time())
    count = 0
    with conn:
        for address in config.addresses.values():
            for position in morpho.positions(address):
                if position.assets == 0:
                    continue  # emptied vault: rules still matter, snapshots don't
                conn.execute(
                    """
                    INSERT INTO snapshots (ts, chain, address, kind, token, source,
                                           amount_native, usd_at_time)
                    VALUES (?, ?, ?, 'position', ?, ?, ?, ?)
                    """,
                    (
                        now,
                        CHAIN,
                        address,
                        position.asset_symbol,
                        SOURCE_MORPHO,
                        str(position.assets),
                        position.assets_usd,
                    ),
                )
                count += 1
    return count


def _snapshot_forty_acres(
    conn: sqlite3.Connection,
    forty_acres: FortyAcresAdapter,
    config: Config,
    prices: PriceResolver,
) -> int:
    """Write active 40acres supply positions (USDC redeemable value)."""
    now = int(time.time())
    count = 0
    with conn:
        for address in config.addresses.values():
            position = forty_acres.position(address)
            if position is None:
                continue
            price = prices.usd_price(position.asset_contract, now)
            conn.execute(
                """
                INSERT INTO snapshots (ts, chain, address, kind, token, source,
                                       amount_native, usd_at_time)
                VALUES (?, ?, ?, 'position', ?, ?, ?, ?)
                """,
                (
                    now,
                    CHAIN,
                    address,
                    position.asset_symbol,
                    SOURCE_40ACRES,
                    str(position.assets),
                    float(position.assets * price) if price is not None else None,
                ),
            )
            count += 1
    return count


def _cursor(conn: sqlite3.Connection, address: str, template: str = CURSOR_KEY_TEMPLATE) -> int:
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?",
        (template.format(address=address),),
    ).fetchone()
    return int(row[0]) if row else 0


def _set_cursor(
    conn: sqlite3.Connection, address: str, block: int, template: str = CURSOR_KEY_TEMPLATE
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            (template.format(address=address), str(block)),
        )


def _merge(total: IngestStats, part: IngestStats) -> None:
    total.events_inserted += part.events_inserted
    total.events_skipped += part.events_skipped
    total.own_transfers_tagged += part.own_transfers_tagged
