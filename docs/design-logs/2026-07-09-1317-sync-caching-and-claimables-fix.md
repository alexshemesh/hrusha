---
date: 2026-07-09T13:17
type: perf
status: implemented
trigger: performance
touches:
  - hrusha/ledger/store.py
  - hrusha/ledger/chain_cache.py
  - hrusha/adapters/forty_acres.py
  - hrusha/adapters/morpho.py
  - hrusha/service/sync.py
  - hrusha/service/vote_scout.py
related:
  - 2026-07-09-1220-vote-discoverability.md
  - 2026-07-06-1117-blockscout-defillama-providers.md
supersedes: null
commit: null
pr: null
---

# Cache immutable chain facts; fix claimables pool coverage

## Context

Measured phase timings (2026-07-09, steady-state cursors, 1 address,
3 veNFTs): the ledger sync is ~22s serial, of which 13.5s (60%) is the
Aerodrome claimables scan — 12 unconditional RewardsSugar.rewards()
chunks per veNFT, even when zero claimables exist. The vote scout scan
is separately ~5–8 minutes: 172 epochsLatest pages over 34,245 pool
indexes, plus ~7 eth_calls per top-100 candidate, plus one serial
GoPlus HTTP call per checked token. Every scan and every sync re-fetch
facts that can never change (token decimals/symbols, pool token0/
token1/kind, completed epoch history) because all caches were
per-process dicts and adapters are constructed fresh per run.

Verified against contracts/RewardsSugar.vy (velodrome-finance/sugar):
`rewards(_limit, _offset, _venft_id)` pages the same global
factory-registry pool-index space as `epochsLatest`. Our
MAX_POOL_CHUNKS=12 × POOLS_PER_CALL=300 covers only the first 3,600 of
34,245 indexes — claimables accrued in newer pools are silently
missing from snapshots. The same contract exposes
`rewardsByAddress(_venft_id, _pool)`, a per-pool variant.

**Revised 2026-09-14, on picking this back up.** Decision 1 shipped
separately while this sat unimplemented — PR #22–25 replaced the
`rewards()` ABI with `rewardsByAddress` and now persists each veNFT's
voted pools in `sync_state` under `aero_vote_pools:{venft_id}`, so the
coverage bug is fixed and the `venft_pools` table this log proposed is
redundant (one owner for that set, and sync already had it). The same
run of work parallelized the scout (ThreadPoolExecutor) and added WAL
plus read-side indexes, which moved the baseline above: the remaining
cost is not the claimables scan but re-deriving immutable facts, every
scan, from chain. Decisions 2 and 3 are what this change implements;
the numbers above are the pre-parallelization baseline and are kept
for the record, not as today's measurement.

## Problem / Goal

Long sync times are the operator's top complaint. Goal: remove
re-fetching of immutable chain facts across runs, and make the
claimables scan both correct (full pool coverage) and cheap.

## Decision

1. ~~**Claimables via voted pools, not index sweeps.**~~ **Landed
   separately** (see above): `AerodromeAdapter.claimables(venft_id,
   pools)` calls `rewardsByAddress` per pool, and sync keeps the
   grow-only pool set in `sync_state`. No `venft_pools` table.
2. **Schema v5: persistent caches of immutable chain facts** in the
   ledger DB (derived state, rebuildable): `token_meta` (contract →
   symbol, decimals — immutable per ERC-20), `pool_meta` (pool →
   token0, token1, kind + epochs_synced_ts), `pool_epochs`
   (pool, epoch_ts → final votes, had_bribes — immutable once the
   epoch completes), `token_checks` (GoPlus verdicts, 7-day TTL
   enforced in code), `token_first_seen` (DefiLlama first price ts,
   hits only). Access through a small `chain_cache` module; adapters
   take an optional cache handle so they stay usable without a DB
   (tests, probes). v4 was taken by the Tier-1 index migration.
3. **Morpho positions fetched once per sync** and passed to both rule
   discovery and snapshotting (was: two identical GraphQL calls per
   address per sync).
4. **Deliberately NOT cached:** running-epoch votes/bribes/fees
   (epochsLatest — live data), pool TVL balanceOf reads, wallet
   balances, current DefiLlama spot prices, Blockscout cursor probes.
   These are the honest real-time core; speeding them up is a
   concurrency problem — since solved by parallelizing them, not by
   caching them.

## Alternatives Considered

- **TTL/skip-window for the claimables scan** (only scan near the
  epoch flip) — rejected: the votes-based fix makes the scan ~1–2s,
  so freshness-trading complexity buys nothing.
- **Deriving chunk count from the registry to fix coverage** — makes
  the sweep correct but ~113 chunks ≈ 50s per veNFT per sync; kept
  only as the one-time bootstrap.
- **Caching the pool-index → alive-gauge map to skip dead epochsLatest
  windows** — rejected as unsafe: gauges can be created later for old
  pool indexes and killed gauges revived, so a "dead window" cache
  silently hides new pools.
- **In-memory caches with a long-lived process** — rejected: sync
  runners construct adapters per run by design (default_sync_runner),
  and the dashboard restarts on deploys; SQLite persistence matches
  the existing price_cache precedent.
- **Do nothing** — the scout scan grows with the registry (34k pool
  indexes and rising weekly); the problem compounds.

## Implementation Notes

- Migration v5 in `hrusha/ledger/store.py` (append-only convention),
  plus `PRAGMA busy_timeout = 5000` in `open_ledger`: WAL keeps
  readers off the writer's back, but the scout's background-thread
  connection and a concurrent sync are two *writers*.
- `hrusha/ledger/chain_cache.py` owns all new-table access; vote_scout
  opens the ledger itself (it previously had no DB access) and
  degrades to full-fetch behaviour when the ledger will not open — the
  cache is an optimization, never a dependency.
- **Threading**: the scout's workers stay "pure RPC/HTTP — no SQLite"
  as parallelization left them. The caller warms plain dicts before
  fanning out and writes back what the workers report (`_FreshFacts`)
  after the join, so no sqlite3 object crosses a thread boundary and
  no lock is needed.
- `pool_meta.epochs_synced_ts` marks history-complete-through-epoch;
  absence of pool_epochs rows alone can't distinguish "young pool"
  from "not fetched". A warm pool's history is reused only when the
  marker covers the last completed epoch, so the refetch is weekly.
- GoPlus verdicts cache stores clean results too ([]), else the
  no-risk majority is refetched forever; NULL-safety: unknown tokens
  (GoPlus never scanned) are cached as clean-with-TTL, matching the
  existing "unknown is not a risk flag" stance. Risk strings are
  stored raw and labelled with the token symbol at use, so a verdict
  cached before the symbol was known still reads correctly later.
- FortyAcresAdapter's ctor metadata (asset/symbol/decimals, 3
  eth_calls) comes from the cache when available; vault → asset
  mapping keyed in `sync_state` (`erc4626_asset:{vault}`).

## Follow-ups

- **Claimables coverage gap, reopened.** Removing the `rewards()` ABI
  means pools a veNFT voted on *before* we started recording votes,
  and no longer votes on, are never queried — unclaimed rewards there
  stay invisible. The one-time registry-wide sweep this log proposed
  as a bootstrap is no longer possible as written. Either restore the
  `rewards()` fragment for a single bootstrap pass per veNFT, or
  derive the historical pool set from the ledger's own `vote` events.
- Doctor check: warn when a veNFT has votes but no recorded pool set
  (bootstrap failed / skipped).
- Per-phase timing in sync logs so regressions are visible without
  re-deriving the baseline by hand.
