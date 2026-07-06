"""Read-side queries over the ledger for the CLI (and later the dashboard)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class TransferRow:
    id: int  # event id, the handle for `hrusha tag`
    ts: int
    kind: str
    token: str
    amount_native: str
    usd_at_time: float | None
    address: str
    counterparty: str | None
    tx_hash: str
    source: str | None
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
        SELECT e.id, e.ts, e.kind, e.token, e.amount_native, e.usd_at_time,
               e.address, e.counterparty, e.tx_hash, e.source,
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


@dataclass(frozen=True)
class NetoRow:
    epoch_id: str  # flip date (Thu, UTC); '?' for events without one
    source: str  # 'untagged' when no rule matched
    income_usd: float  # transfer_in, own-transfers excluded
    spend_usd: float  # transfer_out, own-transfers excluded
    gas_usd: float
    neto_usd: float  # income - gas (spend is informational: swaps aren't losses)
    unpriced_count: int  # events lacking a USD value — the report's honesty note


def neto_by_epoch_source(conn: sqlite3.Connection, since_ts: int = 0) -> list[NetoRow]:
    """Neto per (epoch, source): USD income at event time minus gas.

    Transfers between tracked addresses (tag 'own-transfer') are excluded
    from income/spend entirely. Unpriced events are counted, not valued —
    the report says so rather than silently under-reporting.
    """
    rows = conn.execute(
        """
        SELECT COALESCE(e.epoch_id, '?'),
               COALESCE(e.source, 'untagged'),
               SUM(CASE WHEN e.kind = 'transfer_in' THEN COALESCE(e.usd_at_time, 0) ELSE 0 END),
               SUM(CASE WHEN e.kind = 'transfer_out' THEN COALESCE(e.usd_at_time, 0) ELSE 0 END),
               SUM(CASE WHEN e.kind = 'gas_fee' THEN COALESCE(e.gas_usd, 0) ELSE 0 END),
               SUM(CASE WHEN e.usd_at_time IS NULL THEN 1 ELSE 0 END)
        FROM events e
        WHERE e.ts >= ?
          AND e.id NOT IN (SELECT event_id FROM tags WHERE tag = 'own-transfer')
        GROUP BY 1, 2
        ORDER BY 1 DESC, 3 DESC
        """,
        (since_ts,),
    ).fetchall()
    return [
        NetoRow(
            epoch_id=epoch_id,
            source=source,
            income_usd=income,
            spend_usd=spend,
            gas_usd=gas,
            neto_usd=income - gas,
            unpriced_count=unpriced,
        )
        for epoch_id, source, income, spend, gas, unpriced in rows
    ]


def coins_by_epoch_source(
    conn: sqlite3.Connection, since_ts: int = 0
) -> list[tuple[str, str, str, str, str]]:
    """Native coin amounts per (epoch, source, token, direction) — exact decimals."""
    rows = conn.execute(
        """
        SELECT COALESCE(e.epoch_id, '?'), COALESCE(e.source, 'untagged'),
               e.token, e.kind, e.amount_native
        FROM events e
        WHERE e.ts >= ? AND e.kind IN ('transfer_in', 'transfer_out')
          AND e.id NOT IN (SELECT event_id FROM tags WHERE tag = 'own-transfer')
        """,
        (since_ts,),
    ).fetchall()
    totals: dict[tuple[str, str, str, str], Decimal] = {}
    for epoch_id, source, token, kind, amount in rows:
        key = (epoch_id, source, token, "in" if kind == "transfer_in" else "out")
        totals[key] = totals.get(key, Decimal(0)) + Decimal(amount)
    return sorted(
        (epoch_id, source, token, direction, str(total))
        for (epoch_id, source, token, direction), total in totals.items()
    )


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
