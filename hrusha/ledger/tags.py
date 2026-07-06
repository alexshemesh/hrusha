"""Rule-based auto-tagging, epoch assignment, and manual overrides.

Rules live in the tag_rules table: ordered by priority (lower runs
first), each matches events on any combination of counterparty, token,
contract, kind, or direction, and applies tags plus optionally a
source. Re-running is safe by construction:

- rule-origin tags are deleted and re-derived on every run, so editing
  rules never leaves stale tags behind;
- manual tags (origin 'manual') are never touched, and a rule inserting
  a tag that exists manually is a no-op (UNIQUE(event_id, tag));
- sources are recomputed from scratch; the first matching rule (by
  priority) that carries a source wins;
- gas_fee events inherit the source of their transaction's transfers,
  so "neto per source" can charge gas to the source that caused it.

Epochs follow Aerodrome's weekly flip, Thursday 00:00 UTC. The unix
epoch (1970-01-01) was a Thursday, so epoch boundaries are exact
multiples of 604800 and epoch_id is the flip date of `ts - ts % week`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

OWN_TRANSFER_TAG = "own-transfer"
CLAIM_TAG = "claim"
REINVEST_TAG = "reinvest"
REINVEST_WINDOW_SECONDS = 12 * 3600  # a swap this soon after a claim is a reinvest

SECONDS_PER_WEEK = 604_800  # epochs flip Thu 00:00 UTC; unix epoch was a Thursday

MATCH_KEYS = ("counterparty", "token", "contract", "kind", "direction")


@dataclass
class TagStats:
    rules_run: int = 0
    tags_applied: int = 0
    sources_set: int = 0
    epochs_assigned: int = 0


# -- epochs -------------------------------------------------------------------


def epoch_id_for(ts: int) -> str:
    """Flip date (Thursday, UTC) of the epoch containing `ts`, e.g. '2026-07-02'."""
    start = ts - ts % SECONDS_PER_WEEK
    return datetime.fromtimestamp(start, tz=UTC).strftime("%Y-%m-%d")


def assign_epochs(conn: sqlite3.Connection) -> int:
    """Fill events.epoch_id where missing; register the epochs seen. Returns rows set."""
    with conn:
        cursor = conn.execute(
            """
            UPDATE events
            SET epoch_id = strftime('%Y-%m-%d', (ts - ts % 604800), 'unixepoch')
            WHERE epoch_id IS NULL AND ts > 0
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO epochs (epoch_id, starts_ts, ends_ts)
            SELECT DISTINCT epoch_id, (ts - ts % 604800), (ts - ts % 604800) + 604800
            FROM events WHERE epoch_id IS NOT NULL
            """
        )
    return cursor.rowcount


# -- rules --------------------------------------------------------------------


def retag_all(conn: sqlite3.Connection, tracked_addresses: set[str]) -> TagStats:
    """Recompute all rule-derived tags, sources, and epochs. Manual tags survive."""
    stats = TagStats()
    with conn:
        conn.execute("DELETE FROM tags WHERE origin = 'rule'")
        conn.execute("UPDATE events SET source = NULL")
        stats.tags_applied += _tag_own_transfers(conn, tracked_addresses)
        for rule_id, match_json, tags_csv, source in conn.execute(
            "SELECT id, match_json, tags, source FROM tag_rules"
            " WHERE enabled = 1 ORDER BY priority, id"
        ).fetchall():
            where, params = _rule_where(rule_id, match_json)
            stats.rules_run += 1
            for tag in _split_tags(tags_csv):
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO tags (event_id, tag, origin)"  # noqa: S608
                    f" SELECT id, ?, 'rule' FROM events WHERE {where}",
                    (tag, *params),
                )
                stats.tags_applied += cursor.rowcount
            if source:
                cursor = conn.execute(
                    f"UPDATE events SET source = ? WHERE source IS NULL AND {where}",  # noqa: S608
                    (source, *params),
                )
                stats.sources_set += cursor.rowcount
        stats.sources_set += _inherit_gas_source(conn)
        stats.tags_applied += _tag_reinvests(conn)
    stats.epochs_assigned = assign_epochs(conn)
    return stats


def set_manual_tag(conn: sqlite3.Connection, event_id: int, tag: str) -> bool:
    """Tag an event manually (upgrades an existing rule tag). False if no such event."""
    exists = conn.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone()
    if exists is None:
        return False
    with conn:
        conn.execute(
            "INSERT INTO tags (event_id, tag, origin) VALUES (?, ?, 'manual')"
            " ON CONFLICT (event_id, tag) DO UPDATE SET origin = 'manual'",
            (event_id, tag),
        )
    return True


def add_rule(
    conn: sqlite3.Connection,
    priority: int,
    match: dict[str, str],
    tags: list[str],
    source: str | None = None,
) -> int:
    """Insert a tag rule; `match` keys must be in MATCH_KEYS. Returns the rule id."""
    unknown = set(match) - set(MATCH_KEYS)
    if unknown:
        raise ValueError(f"unknown match keys: {', '.join(sorted(unknown))}")
    if not match:
        raise ValueError("a rule must match on at least one field")
    with conn:
        cursor = conn.execute(
            "INSERT INTO tag_rules (priority, match_json, tags, source) VALUES (?, ?, ?, ?)",
            (priority, json.dumps(match), ",".join(tags), source),
        )
    return cursor.lastrowid


# -- internals ----------------------------------------------------------------


def _rule_where(rule_id: int, match_json: str) -> tuple[str, list[str]]:
    try:
        match = json.loads(match_json)
    except ValueError as exc:
        raise ValueError(f"tag rule {rule_id} has invalid match_json") from exc
    clauses, params = [], []
    for key, value in match.items():
        if key == "direction":
            clauses.append("kind = ?")
            params.append(f"transfer_{value}")
        elif key in ("counterparty", "contract"):
            clauses.append(f"{key} = ?")
            params.append(str(value).lower())
        elif key in ("token", "kind"):
            clauses.append(f"{key} = ?")
            params.append(str(value))
        else:
            raise ValueError(f"tag rule {rule_id} matches on unknown field {key!r}")
    if not clauses:
        raise ValueError(f"tag rule {rule_id} matches nothing")
    return " AND ".join(clauses), params


def _split_tags(tags_csv: str) -> list[str]:
    return [tag.strip() for tag in tags_csv.split(",") if tag.strip()]


def _tag_own_transfers(conn: sqlite3.Connection, tracked: set[str]) -> int:
    """Builtin rule: transfers between tracked addresses are not income/spend."""
    if not tracked:
        return 0
    placeholders = ",".join("?" * len(tracked))
    cursor = conn.execute(
        "INSERT OR IGNORE INTO tags (event_id, tag, origin)"  # noqa: S608
        f" SELECT id, '{OWN_TRANSFER_TAG}', 'rule' FROM events"
        f" WHERE counterparty IN ({placeholders})",
        tuple(sorted(tracked)),
    )
    return cursor.rowcount


def _inherit_gas_source(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        UPDATE events SET source = (
            SELECT e2.source FROM events e2
            WHERE e2.tx_hash = events.tx_hash AND e2.source IS NOT NULL
            ORDER BY e2.id LIMIT 1
        )
        WHERE kind = 'gas_fee' AND source IS NULL
          AND EXISTS (
            SELECT 1 FROM events e3
            WHERE e3.tx_hash = events.tx_hash AND e3.source IS NOT NULL
          )
        """
    )
    return cursor.rowcount


def _tag_reinvests(conn: sqlite3.Connection) -> int:
    """An outgoing transfer soon after a claim, from the same address, is a reinvest."""
    cursor = conn.execute(
        f"""
        INSERT OR IGNORE INTO tags (event_id, tag, origin)
        SELECT DISTINCT out_e.id, '{REINVEST_TAG}', 'rule'
        FROM events out_e
        JOIN tags claim_t ON claim_t.tag = '{CLAIM_TAG}'
        JOIN events in_e ON in_e.id = claim_t.event_id
        WHERE out_e.kind = 'transfer_out'
          AND out_e.address = in_e.address
          AND out_e.ts BETWEEN in_e.ts AND in_e.ts + {REINVEST_WINDOW_SECONDS}
        """  # noqa: S608 — interpolations are module constants, not user input
    )
    return cursor.rowcount
