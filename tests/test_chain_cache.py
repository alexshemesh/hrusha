"""Persistent chain-fact caches (schema v5) and their staleness rules."""

import sqlite3

import pytest

from hrusha.ledger.chain_cache import GOPLUS_TTL_SECONDS, ChainCache
from hrusha.ledger.store import SCHEMA_MIGRATIONS, SCHEMA_VERSION, open_ledger, schema_version

POOL = "0xPoolAddress"
TOKEN = "0xTokenAddress"
VAULT = "0xVaultAddress"


@pytest.fixture
def cache(ledger):
    return ChainCache(ledger)


# -- migration ---------------------------------------------------------------


def test_v5_applies_to_a_v4_ledger_without_touching_its_data(tmp_path):
    """The cache tables are additive: an existing ledger keeps everything."""
    db_path = tmp_path / "ledger.db"
    conn = sqlite3.connect(db_path)
    for migration in SCHEMA_MIGRATIONS[:4]:
        conn.executescript(migration)
    conn.execute("PRAGMA user_version = 4")
    conn.execute(
        """
        INSERT INTO events (ts, chain, tx_hash, log_index, block, kind, token,
                            amount_native, address)
        VALUES (1720000000, 'base', '0xabc', 0, 1, 'transfer_in', 'ETH', '1.0', '0xowner')
        """
    )
    conn.commit()
    conn.close()

    migrated = open_ledger(db_path)
    assert schema_version(migrated) == SCHEMA_VERSION
    assert migrated.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    for table in ("token_meta", "pool_meta", "pool_epochs", "token_checks", "token_first_seen"):
        migrated.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 — fixed names
    migrated.close()


def test_open_ledger_waits_for_a_busy_writer(ledger):
    """The scout writes the cache from a background thread while a sync may
    hold the ledger; a busy timeout turns a crash into a wait."""
    assert ledger.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


# -- token metadata ----------------------------------------------------------


def test_token_meta_round_trips_case_insensitively(cache):
    cache.store_token_meta(TOKEN, "AERO", 18)
    assert cache.token_meta(TOKEN.lower()) == ("AERO", 18)
    assert cache.token_meta(TOKEN.upper()) == ("AERO", 18)
    assert cache.token_meta("0xNeverSeen") is None


def test_token_meta_bulk_read_and_write(cache):
    cache.store_token_meta_many({TOKEN: ("AERO", 18), "0xOther": ("USDC", 6)})
    assert cache.token_meta_all() == {TOKEN.lower(): ("AERO", 18), "0xother": ("USDC", 6)}
    cache.store_token_meta_many({})  # no-op, not an error


# -- pool metadata and epoch history ----------------------------------------


def test_pool_meta_restore_keeps_the_history_marker(cache):
    """Metadata and history are written by different paths: re-storing the
    pool's tokens must not silently reset how far its history was filled."""
    cache.store_pool_meta(POOL, "0xA", "0xB", "vAMM")
    cache.store_pool_epochs(POOL, [(1000, 5.0, True)], synced_through_ts=2000)
    cache.store_pool_meta(POOL, "0xA", "0xB", "sAMM")  # kind corrected later

    meta = cache.pool_meta(POOL)
    assert (meta.token0, meta.token1, meta.kind) == ("0xa", "0xb", "sAMM")
    assert meta.epochs_synced_ts == 2000


def test_pool_epochs_come_back_newest_first(cache):
    cache.store_pool_meta(POOL, "0xA", "0xB", "vAMM")
    cache.store_pool_epochs(POOL, [(1000, 5.0, True), (3000, 7.0, False)], synced_through_ts=4000)
    assert cache.pool_epochs(POOL) == [(3000, 7.0, False), (1000, 5.0, True)]


def test_a_pool_with_no_completed_epochs_is_still_marked_checked(cache):
    """'young pool, checked' and 'never fetched' must stay distinguishable —
    otherwise every scan re-fetches the history of every new pool."""
    cache.store_pool_meta(POOL, "0xA", "0xB", "vAMM")
    cache.store_pool_epochs(POOL, [], synced_through_ts=2000)
    assert cache.pool_epochs(POOL) == []
    assert cache.pool_meta(POOL).epochs_synced_ts == 2000


def test_pool_meta_many_warms_only_what_was_asked_for(cache):
    cache.store_pool_meta(POOL, "0xA", "0xB", "vAMM")
    cache.store_pool_meta("0xOtherPool", "0xC", "0xD", "sAMM")
    warmed = cache.pool_meta_many([POOL, "0xNeverSeen"])
    assert set(warmed) == {POOL.lower()}
    assert cache.pool_meta_many([]) == {}


# -- GoPlus verdicts (TTL) ---------------------------------------------------


def test_clean_verdicts_cache_too(cache):
    """Most tokens are clean; without caching that, the no-risk majority is
    re-fetched forever at one HTTP call each."""
    cache.store_token_risks(TOKEN, [], now=1000)
    assert cache.token_risks(TOKEN, now=1000) == ()  # cached-clean, not a miss


def test_verdicts_expire(cache):
    cache.store_token_risks(TOKEN, ["is_honeypot"], now=1000)
    assert cache.token_risks(TOKEN, now=1000 + GOPLUS_TTL_SECONDS) == ("is_honeypot",)
    assert cache.token_risks(TOKEN, now=1000 + GOPLUS_TTL_SECONDS + 1) is None  # stale = refetch


def test_unknown_token_is_a_miss_not_a_clean_verdict(cache):
    assert cache.token_risks("0xNeverChecked", now=1000) is None


# -- DefiLlama first-seen ----------------------------------------------------


def test_first_seen_returns_only_hits(cache):
    cache.store_first_seen({TOKEN: 1690000000})
    assert cache.first_seen([TOKEN, "0xUnpriced"]) == {TOKEN.lower(): 1690000000}
    assert cache.first_seen([]) == {}


# -- ERC-4626 vault asset ----------------------------------------------------


def test_erc4626_asset_round_trips(cache):
    assert cache.erc4626_asset(VAULT) is None
    cache.store_erc4626_asset(VAULT, "0xUSDC")
    assert cache.erc4626_asset(VAULT) == "0xusdc"
