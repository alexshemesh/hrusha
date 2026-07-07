"""Read-side queries over the ledger for the CLI (and later the dashboard)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from hrusha.ledger.tags import (
    DEPOSIT_TAG,
    LOCK_TAG,
    NON_FLOW_TAGS,
    OWN_TRANSFER_TAG,
    PURCHASE_TAG,
    SWAP_TAG,
    UNLOCK_TAG,
    WITHDRAW_TAG,
)

_NON_FLOW_PLACEHOLDERS = ",".join("?" * len(NON_FLOW_TAGS))


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
    token_id: str | None = None  # ERC-721 id; None for fungible transfers


@dataclass(frozen=True)
class FeeSummary:
    tx_count: int
    total_eth: str
    total_usd: float  # sum over priced fees only
    unpriced_count: int


def recent_transfers(
    conn: sqlite3.Connection,
    limit: int = 50,
    epoch_id: str | None = None,
    source: str | None = None,
    tag: str | None = None,
) -> list[TransferRow]:
    """Latest transfers, optionally narrowed — the income report's drill-down."""
    clauses = ["e.kind IN ('transfer_in', 'transfer_out')"]
    params: list = []
    if epoch_id is not None:
        clauses.append("e.epoch_id = ?")
        params.append(epoch_id)
    if source is not None:
        clauses.append("COALESCE(e.source, 'untagged') = ?")
        params.append(source)
    if tag is not None:
        clauses.append("e.id IN (SELECT event_id FROM tags WHERE tag = ?)")
        params.append(tag)
    rows = conn.execute(
        f"""
        SELECT e.id, e.ts, e.kind, e.token, e.amount_native, e.usd_at_time,
               e.address, e.counterparty, e.tx_hash, e.source,
               COALESCE(GROUP_CONCAT(t.tag, ','), ''), e.token_id
        FROM events e
        LEFT JOIN tags t ON t.event_id = e.id
        WHERE {" AND ".join(clauses)}
        GROUP BY e.id
        ORDER BY e.ts DESC, e.id DESC
        LIMIT ?
        """,  # noqa: S608 — clauses are literals above; values are bound
        (*params, limit),
    ).fetchall()
    return [TransferRow(*row) for row in rows]


@dataclass(frozen=True)
class SnapshotRow:
    ts: int
    address: str
    kind: str  # balance|position|claimable
    token: str
    source: str | None
    amount_native: str
    usd_at_time: float | None


SNAPSHOT_SYNC_WINDOW_SECONDS = 600  # one sync writes its snapshot groups seconds apart

# grouping policy for the strategy view (display-level, never stored):
# rebases are anti-dilution income of the voting strategy, and veNFT
# purchases are its capital basis even though their payment legs carry
# no source (they go to marketplaces/sellers, not to Aerodrome)
_REBASE_SOURCE = "aerodrome-rebase"
_VOTING_SOURCE = "aerodrome-voting"


@dataclass(frozen=True)
class StrategyRow:
    source: str
    deposited_usd: float  # wallet -> strategy: deposits, locks, veNFT purchases
    withdrawn_usd: float  # strategy -> wallet principal: withdrawals, unlocks
    income_usd: float  # strategy -> wallet income: claims etc.
    other_out_usd: float  # source-attributed spending that is none of the above
    gas_usd: float
    position_usd: float  # latest position + claimable snapshots
    profit_usd: float  # income + withdrawn + position - deposited - other - gas
    unpriced_count: int  # legs that count toward flows but have no USD value
    # in-kind decomposition for vault-style strategies (auto-compounders pay
    # no income events — their yield hides inside withdrawn+holding, and the
    # USD columns mix it with the asset's own price moves):
    #   yield_items: (token, net coins, value at latest cached price) — net
    #     coins = withdrawals + holding - deposits, in the asset's own units
    #   price_effect_usd: profit + gas - income - yield: what holding the
    #     asset through the period did to the USD number
    # both None/empty for income-style strategies (no deposit/withdraw legs)
    yield_items: tuple[tuple[str, float, float | None], ...] = ()
    yield_usd: float | None = None
    price_effect_usd: float | None = None


def strategy_summary(
    conn: sqlite3.Connection, snapshots: list[SnapshotRow] | None = None
) -> list[StrategyRow]:
    """Lifetime profit per strategy: everything that crossed the wallet<->
    strategy boundary (USD at event time) plus what the strategy holds now.

    Swap legs are skipped — they are the same value changing form inside a
    tx whose meaningful leg (the deposit/withdraw side) is counted once.
    Share mint/burn legs are IN/OUT mirrors of those and fall out of every
    bucket by direction. Unpriced legs are counted, not valued.
    """
    snapshots = snapshots if snapshots is not None else latest_snapshots(conn)
    totals: dict[str, dict[str, float]] = {}

    def bucket(source: str) -> dict[str, float]:
        return totals.setdefault(
            source,
            {"deposited": 0.0, "withdrawn": 0.0, "income": 0.0, "other_out": 0.0,
             "gas": 0.0, "position": 0.0, "unpriced": 0},
        )  # fmt: skip

    # in-kind flows per source: symbol -> {coins, contract}. Only true
    # deposit/withdraw ASSET legs count (locks/purchases are aerodrome
    # capital in other tokens; share mint/burn mirrors are swap-skipped)
    in_kind: dict[str, dict[str, dict]] = {}

    rows = conn.execute(
        """
        SELECT e.kind, COALESCE(e.source, ''), e.usd_at_time, e.gas_usd,
               e.token, e.contract, e.amount_native,
               COALESCE(GROUP_CONCAT(t.tag, ','), '')
        FROM events e
        LEFT JOIN tags t ON t.event_id = e.id
        WHERE e.kind IN ('transfer_in', 'transfer_out', 'gas_fee')
        GROUP BY e.id
        """
    ).fetchall()
    for kind, source, usd, gas_usd, token, contract, amount, tags_csv in rows:
        tags = set(tags_csv.split(",")) if tags_csv else set()
        if PURCHASE_TAG in tags:
            source = _VOTING_SOURCE
        elif source == _REBASE_SOURCE:
            source = _VOTING_SOURCE
        if not source or OWN_TRANSFER_TAG in tags:
            continue
        b = bucket(source)
        if kind == "gas_fee":
            b["gas"] += gas_usd or 0.0  # gas_usd is the canonical fee value
            continue
        outgoing = kind == "transfer_out"
        if outgoing and tags & {DEPOSIT_TAG, LOCK_TAG, PURCHASE_TAG}:
            slot = "deposited"
            if DEPOSIT_TAG in tags:
                _add_in_kind(in_kind, source, token, contract, -float(amount))
        elif not outgoing and tags & {WITHDRAW_TAG, UNLOCK_TAG}:
            slot = "withdrawn"
            if WITHDRAW_TAG in tags:
                _add_in_kind(in_kind, source, token, contract, float(amount))
        elif SWAP_TAG in tags:
            continue  # form change; the counted leg of the tx is elsewhere
        elif outgoing:
            slot = "other_out"
        else:
            slot = "income"
        if usd is None:
            b["unpriced"] += 1
        else:
            b[slot] += usd

    for row in snapshots:
        if row.kind not in ("position", "claimable") or not row.source:
            continue
        source = _VOTING_SOURCE if row.source == _REBASE_SOURCE else row.source
        bucket(source)["position"] += row.usd_at_time or 0.0
        # held coins complete the in-kind cycle: yield = out + held - in.
        # claimables stay out — they become income the day they arrive
        family = in_kind.get(source)
        token = "WETH" if row.token == "ETH" else row.token  # noqa: S105 — token symbol, not a secret
        if row.kind == "position" and family and token in family:
            family[token]["coins"] += float(row.amount_native)

    result = []
    for source, b in totals.items():
        profit = (
            b["income"] + b["withdrawn"] + b["position"]
            - b["deposited"] - b["other_out"] - b["gas"]
        )  # fmt: skip
        items, yield_usd = _valued_yield(conn, in_kind.get(source))
        result.append(
            StrategyRow(
                source=source,
                deposited_usd=b["deposited"],
                withdrawn_usd=b["withdrawn"],
                income_usd=b["income"],
                other_out_usd=b["other_out"],
                gas_usd=b["gas"],
                position_usd=b["position"],
                profit_usd=profit,
                unpriced_count=int(b["unpriced"]),
                yield_items=items,
                yield_usd=yield_usd,
                price_effect_usd=(
                    profit + b["gas"] - b["income"] - yield_usd if yield_usd is not None else None
                ),
            )
        )
    return sorted(result, key=lambda r: -r.profit_usd)


def _add_in_kind(
    in_kind: dict, source: str, token: str, contract: str | None, coins: float
) -> None:
    token = "WETH" if token == "ETH" else token  # noqa: S105 — 1:1 symbol merge, not a secret
    entry = in_kind.setdefault(source, {}).setdefault(token, {"coins": 0.0, "contract": None})
    entry["coins"] += coins
    entry["contract"] = entry["contract"] or contract


def _valued_yield(
    conn: sqlite3.Connection, family: dict | None
) -> tuple[tuple[tuple[str, float, float | None], ...], float | None]:
    """In-kind yield items valued at the latest cached daily price.

    yield_usd is None (undecomposable) when the strategy has no
    deposit/withdraw legs at all, or when any asset lacks a price —
    a partial sum would silently misattribute the rest to price effect."""
    if not family:
        return (), None
    items = []
    total = 0.0
    fully_priced = True
    for token, entry in sorted(family.items()):
        keys = (entry["contract"], "ETH" if token == "WETH" else None, token)  # noqa: S105
        price = _latest_cached_price(conn, keys)
        value = entry["coins"] * price if price is not None else None
        if value is None:
            fully_priced = False
        else:
            total += value
        if abs(entry["coins"]) < 1e-6 or (value is not None and abs(value) < 0.01):
            continue  # dust: deposit/withdraw round-trips that net to nothing
        items.append((token, entry["coins"], value))
    return tuple(items), (total if fully_priced else None)


def _latest_cached_price(conn: sqlite3.Connection, keys: tuple[str | None, ...]) -> float | None:
    for key in keys:
        if not key:
            continue
        row = conn.execute(
            "SELECT usd FROM price_cache WHERE token = ? AND usd IS NOT NULL"
            " ORDER BY day DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row:
            return float(row[0])
    return None


def latest_snapshots(conn: sqlite3.Connection) -> list[SnapshotRow]:
    """Snapshots from the most recent sync (all rows within its write window)."""
    newest = conn.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
    if newest is None:
        return []
    rows = conn.execute(
        """
        SELECT ts, address, kind, token, source, amount_native, usd_at_time
        FROM snapshots WHERE ts > ?
        ORDER BY kind, usd_at_time DESC
        """,
        (newest - SNAPSHOT_SYNC_WINDOW_SECONDS,),
    ).fetchall()
    return [SnapshotRow(*row) for row in rows]


@dataclass(frozen=True)
class NetoRow:
    epoch_id: str  # flip date (Thu, UTC); '?' for events without one
    source: str  # 'untagged' when no rule matched
    income_usd: float  # transfer_in, own-transfers excluded
    spend_usd: float  # transfer_out, own-transfers excluded
    gas_usd: float
    neto_usd: float  # income - gas (spend is informational: swaps aren't losses)
    unpriced_count: int  # events lacking a USD value — the report's honesty note


def neto_by_epoch_source(
    conn: sqlite3.Connection, since_ts: int = 0, until_ts: int | None = None
) -> list[NetoRow]:
    """Neto per (epoch, source): USD income at event time minus gas.

    Non-flow events (NON_FLOW_TAGS: own-transfers, swap legs, lock/unlock
    escrow moves) are excluded from income/spend — money changing place or
    form is not being made or lost. Unpriced events are counted, not
    valued — the report says so rather than silently under-reporting.
    """
    rows = conn.execute(
        f"""
        SELECT COALESCE(e.epoch_id, '?'),
               COALESCE(e.source, 'untagged'),
               SUM(CASE WHEN e.kind = 'transfer_in' THEN COALESCE(e.usd_at_time, 0) ELSE 0 END),
               SUM(CASE WHEN e.kind = 'transfer_out' THEN COALESCE(e.usd_at_time, 0) ELSE 0 END),
               SUM(CASE WHEN e.kind = 'gas_fee' THEN COALESCE(e.gas_usd, 0) ELSE 0 END),
               SUM(CASE WHEN e.usd_at_time IS NULL THEN 1 ELSE 0 END)
        FROM events e
        WHERE e.ts >= ? AND e.ts < ?
          AND e.id NOT IN (
            SELECT event_id FROM tags WHERE tag IN ({_NON_FLOW_PLACEHOLDERS})
          )
        GROUP BY 1, 2
        ORDER BY 1 DESC, 3 DESC
        """,  # noqa: S608 — placeholders only; values bound below
        (since_ts, until_ts if until_ts is not None else 2**53, *NON_FLOW_TAGS),
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
    conn: sqlite3.Connection, since_ts: int = 0, until_ts: int | None = None
) -> list[tuple[str, str, str, str, str]]:
    """Native coin amounts per (epoch, source, token, direction) — exact decimals."""
    rows = conn.execute(
        f"""
        SELECT COALESCE(e.epoch_id, '?'), COALESCE(e.source, 'untagged'),
               e.token, e.kind, e.amount_native
        FROM events e
        WHERE e.ts >= ? AND e.ts < ? AND e.kind IN ('transfer_in', 'transfer_out')
          AND e.id NOT IN (
            SELECT event_id FROM tags WHERE tag IN ({_NON_FLOW_PLACEHOLDERS})
          )
        """,  # noqa: S608 — placeholders only; values bound below
        (since_ts, until_ts if until_ts is not None else 2**53, *NON_FLOW_TAGS),
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
