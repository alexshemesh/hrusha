"""Persistent caches of immutable (or slowly-changing) chain facts.

Sync and the vote scout used to re-fetch these every run because every
cache was a per-process dict and adapters are constructed per run. The
facts cached here are immutable by construction — ERC-20 symbol and
decimals, a pool's tokens and kind, a completed epoch's final votes —
except for GoPlus verdicts, which get a TTL instead.

All tables live in the ledger DB (schema v5): the ledger is derived
state already, so a rebuild simply re-warms these. Writers use short
transactions; `open_ledger` sets a busy timeout so the scout's
background-thread connection and a concurrent sync don't fail on lock.

Threading: a ChainCache belongs to one connection and therefore to one
thread. The vote scout's parallel workers stay "pure RPC/HTTP — no
SQLite" (see service/vote_scout.py): the caller warms plain dicts from
the cache before fanning out and flushes new entries back afterwards,
so no sqlite3 object ever crosses a thread boundary.

Voted pools are deliberately NOT cached here — sync owns that set in
`sync_state` under `aero_vote_pools:{venft_id}`, written on the same
pass that reads a veNFT's votes (see service/sync.py).

Design log: docs/design-logs/2026-07-09-1317-sync-caching-and-claimables-fix.md
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

GOPLUS_TTL_SECONDS = 7 * 86400  # token-safety verdicts are slow-changing, not immutable
ERC4626_ASSET_KEY = "erc4626_asset:{vault}"


@dataclass(frozen=True)
class PoolMeta:
    pool: str
    token0: str
    token1: str
    kind: str  # 'CL<tickSpacing>' | 'sAMM' | 'vAMM' | '?'
    epochs_synced_ts: int  # epoch start the pool's history is complete through


class ChainCache:
    """Facade over the schema-v5 cache tables; one per open ledger connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- token metadata (immutable) --------------------------------------------

    def token_meta(self, token: str) -> tuple[str, int] | None:
        """(symbol, decimals) or None when never seen."""
        row = self._conn.execute(
            "SELECT symbol, decimals FROM token_meta WHERE token = ?", (token.lower(),)
        ).fetchone()
        return (row[0], int(row[1])) if row else None

    def store_token_meta(self, token: str, symbol: str, decimals: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO token_meta (token, symbol, decimals) VALUES (?, ?, ?)",
                (token.lower(), symbol, decimals),
            )

    def token_meta_all(self) -> dict[str, tuple[str, int]]:
        """Every cached token, for warming a scan's in-memory dict in one read.

        The table holds one row per ERC-20 the scout has ever described
        (thousands at most); a scan touches a large, unpredictable slice of
        it, so one read beats a parameterized IN over guessed addresses.
        """
        return {
            row[0]: (row[1], int(row[2]))
            for row in self._conn.execute("SELECT token, symbol, decimals FROM token_meta")
        }

    def store_token_meta_many(self, meta_by_token: dict[str, tuple[str, int]]) -> None:
        if not meta_by_token:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO token_meta (token, symbol, decimals) VALUES (?, ?, ?)",
                [
                    (token.lower(), symbol, decimals)
                    for token, (symbol, decimals) in meta_by_token.items()
                ],
            )

    # -- pool metadata (immutable) + epoch history sync marker -----------------

    def pool_meta(self, pool: str) -> PoolMeta | None:
        row = self._conn.execute(
            "SELECT token0, token1, kind, epochs_synced_ts FROM pool_meta WHERE pool = ?",
            (pool.lower(),),
        ).fetchone()
        if row is None:
            return None
        return PoolMeta(pool.lower(), row[0], row[1], row[2], int(row[3]))

    def pool_meta_many(self, pools: Iterable[str]) -> dict[str, PoolMeta]:
        """Warm a scan's candidate set in one read (~100 pools per scan)."""
        pools = [pool.lower() for pool in pools]
        if not pools:
            return {}
        placeholders = ",".join("?" * len(pools))
        return {
            row[0]: PoolMeta(row[0], row[1], row[2], row[3], int(row[4]))
            for row in self._conn.execute(
                "SELECT pool, token0, token1, kind, epochs_synced_ts FROM pool_meta"  # noqa: S608
                f" WHERE pool IN ({placeholders})",
                pools,
            )
        }

    def store_pool_meta(self, pool: str, token0: str, token1: str, kind: str) -> None:
        with self._conn:
            self._conn.execute(
                # keep epochs_synced_ts on re-store: metadata and history
                # are written by different code paths
                """
                INSERT INTO pool_meta (pool, token0, token1, kind) VALUES (?, ?, ?, ?)
                ON CONFLICT (pool) DO UPDATE SET
                    token0 = excluded.token0, token1 = excluded.token1, kind = excluded.kind
                """,
                (pool.lower(), token0.lower(), token1.lower(), kind),
            )

    # -- completed epoch history (immutable once the epoch closes) -------------

    def pool_epochs(self, pool: str) -> list[tuple[int, float, bool]]:
        """(epoch_ts, final_votes, had_bribes), newest first."""
        rows = self._conn.execute(
            "SELECT epoch_ts, votes, had_bribes FROM pool_epochs"
            " WHERE pool = ? ORDER BY epoch_ts DESC",
            (pool.lower(),),
        ).fetchall()
        return [(int(ts), float(votes), bool(bribes)) for ts, votes, bribes in rows]

    def store_pool_epochs(
        self,
        pool: str,
        rows: Iterable[tuple[int, float, bool]],
        synced_through_ts: int,
    ) -> None:
        """Record completed epochs and mark history complete through
        `synced_through_ts` (the running epoch's start). A pool with no
        completed epochs still gets the marker — 'young pool, checked' and
        'never fetched' must stay distinguishable."""
        pool = pool.lower()
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO pool_epochs (pool, epoch_ts, votes, had_bribes)"
                " VALUES (?, ?, ?, ?)",
                [(pool, ts, votes, int(bribes)) for ts, votes, bribes in rows],
            )
            self._conn.execute(
                "UPDATE pool_meta SET epochs_synced_ts = ? WHERE pool = ?",
                (synced_through_ts, pool),
            )

    # -- GoPlus verdicts (TTL) --------------------------------------------------

    def token_risks(self, token: str, now: int) -> tuple[str, ...] | None:
        """Cached risk strings ((), i.e. clean, included) or None when stale/missing."""
        row = self._conn.execute(
            "SELECT checked_ts, risks FROM token_checks WHERE token = ?", (token.lower(),)
        ).fetchone()
        if row is None or now - int(row[0]) > GOPLUS_TTL_SECONDS:
            return None
        return tuple(json.loads(row[1]))

    def store_token_risks(self, token: str, risks: Iterable[str], now: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO token_checks (token, checked_ts, risks) VALUES (?, ?, ?)",
                (token.lower(), now, json.dumps(list(risks))),
            )

    # -- DefiLlama first-seen (immutable; hits only — a token DefiLlama can't
    # price today may gain a price later) ---------------------------------------

    def first_seen(self, tokens: Iterable[str]) -> dict[str, int]:
        tokens = [t.lower() for t in tokens]
        if not tokens:
            return {}
        placeholders = ",".join("?" * len(tokens))
        return {
            row[0]: int(row[1])
            for row in self._conn.execute(
                f"SELECT token, first_ts FROM token_first_seen"  # noqa: S608
                f" WHERE token IN ({placeholders})",
                tokens,
            )
        }

    def store_first_seen(self, first_ts_by_token: dict[str, int]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO token_first_seen (token, first_ts) VALUES (?, ?)",
                [(token.lower(), ts) for token, ts in first_ts_by_token.items()],
            )

    # -- ERC-4626 vault asset (immutable; lives in sync_state, not a new table) -

    def erc4626_asset(self, vault: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM sync_state WHERE key = ?",
            (ERC4626_ASSET_KEY.format(vault=vault.lower()),),
        ).fetchone()
        return row[0] if row else None

    def store_erc4626_asset(self, vault: str, asset: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
                (ERC4626_ASSET_KEY.format(vault=vault.lower()), asset.lower()),
            )
