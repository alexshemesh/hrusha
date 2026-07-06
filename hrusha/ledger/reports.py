"""Read-side queries over the ledger for the CLI (and later the dashboard)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TransferRow:
    ts: int
    kind: str
    token: str
    amount_native: str
    usd_at_time: float | None
    address: str
    counterparty: str | None
    tx_hash: str
    tags: str  # comma-joined, '' when untagged


@dataclass(frozen=True)
class FeeSummary:
    tx_count: int
    total_eth: str
    total_usd: float  # sum over priced fees only
    unpriced_count: int


def recent_transfers(conn: sqlite3.Connection, limit: int = 50) -> list[TransferRow]:
    rows = conn.execute(
        """
        SELECT e.ts, e.kind, e.token, e.amount_native, e.usd_at_time,
               e.address, e.counterparty, e.tx_hash,
               COALESCE(GROUP_CONCAT(t.tag, ','), '')
        FROM events e
        LEFT JOIN tags t ON t.event_id = e.id
        WHERE e.kind IN ('transfer_in', 'transfer_out')
        GROUP BY e.id
        ORDER BY e.ts DESC, e.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [TransferRow(*row) for row in rows]


def fee_summary(conn: sqlite3.Connection, since_ts: int = 0) -> FeeSummary:
    count, total_usd, unpriced = conn.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(gas_usd), 0),
               SUM(CASE WHEN gas_usd IS NULL THEN 1 ELSE 0 END)
        FROM events WHERE kind = 'gas_fee' AND ts >= ?
        """,
        (since_ts,),
    ).fetchone()
    # exact decimal sum in Python: REAL would drift and gas amounts are tiny
    amounts = conn.execute(
        "SELECT amount_native FROM events WHERE kind = 'gas_fee' AND ts >= ?", (since_ts,)
    ).fetchall()
    total_eth = sum((Decimal(a[0]) for a in amounts), Decimal(0))
    return FeeSummary(
        tx_count=count or 0,
        total_eth=str(total_eth),
        total_usd=total_usd or 0.0,
        unpriced_count=unpriced or 0,
    )
