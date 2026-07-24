---
date: 2026-07-24T12:00
type: architecture
status: proposed
trigger: architecture | api-change | new-pattern
touches:
  - hrusha/ledger/store.py
  - hrusha/ledger/reports.py
  - hrusha/service/sync.py
  - hrusha/providers/alchemy_rpc.py
  - hrusha/cli.py
  - hrusha/service/app.py
related:
  - 2026-07-08-1200-invest-scout-suggestions.md
supersedes: null
commit: null
pr: null
---

# Faster reads, faster syncs, agent-queryable ledger

## Context
hrusha's direction shift: same goal (personal crypto income monitor on
Base) but prioritize (A) fast queries over already-synced data, (B) faster
syncs, and (C) letting the agent run custom queries against the ledger
instead of re-fetching from chain.

Current data path: Alchemy/Blockscout -> sync.py (serial per address,
prices resolved inline) -> SQLite ledger (store.py, untuned) -> reports.py
(aggregate-on-every-call) -> CLI/dashboard.

## Problem / Goal
- Dashboard/report queries re-aggregate the full event set on every call.
- SQLite is opened with default journal/synchronous settings (DELETE/FULL).
- Sync is serial across addresses; price resolution blocks the sync
  critical path.
- Only fixed CLI commands exist; the agent cannot ask the ledger an
  ad-hoc question and must re-fetch from chain (wasteful, per hrusha-run
  skill's golden rule).

## Decision
Three tiers, built in order; each tier is independently shippable.

### Tier 1 — faster reads (no behavior change)
- A1: SQLite PRAGMAs in open_ledger: journal_mode=WAL,
  synchronous=NORMAL, mmap_size=256MiB, cache_size=64MiB,
  temp_store=MEMORY. Safe vs corruption (only last txn lost on power
  cut); writers still serialized.
- A4: covering indexes for report queries: (address, ts),
  (source, epoch_id, ts). Cheap to maintain.

### Tier 2 — faster syncs
- B1: parallelize address fetches via ThreadPoolExecutor (~4 workers
  capped, respects Alchemy rate limits). Fetches fan out; writes stay
  single-threaded. Per-address block cursor unchanged.
- B3 (later): decouple price resolution from sync — write events with
  usd_at_time=NULL, repricing runs async. report shows pending.

### Tier 3 — agent-queryable ledger
- C1: structured read-only query API (hrusha query CLI + GET /query)
  with parameterized filters (token, source, kind, address, tag,
  since/until, limit) -> JSON. No raw SQL surface; row-limited.
- C2: expose as a pi tool/skill so the agent composes filters from
  natural language and queries the ledger directly.

### Deferred (measure first)
- A2: materialized summary tables (epoch_summary, daily_pnl) maintained
  incrementally — only if dashboard still slow after Tier 1.
- A3: in-process query cache in app.py (TTL, invalidate on sync).
- A5: move Python-side bucketing into SQL.

## Alternatives Considered
- **Postgres instead of tuned SQLite** — rejected; single-user app, SQLite
  with WAL is enough and removes an operating dependency.
- **Raw SQL query endpoint** — rejected for C1; parameterized filters are
  safer and sufficient. Raw SQL (C3) only if filters prove too rigid.
- **Full async sync rewrite** — rejected; ThreadPoolExecutor on the
  existing sync is far less churn for most of the win.
- **Materialized summaries first** — rejected as Tier 1; added complexity
  before proving the trivial PRAGMA+index wins are insufficient.

## Implementation Notes
- Tier 1 is pure storage-layer: open_ledger PRAGMAs + CREATE INDEX IF NOT
  EXISTS in a new schema migration. No query changes.
- Preserve invariants: sync stays idempotent + resumable (per-address
  cursor, dedup constraint). All parallelism on fetch side, writes serial.
- All new query surfaces read-only, parameterized, row-limited.
- Measure before building Tier 2/3: time `hrusha report` and a full sync
  before and after Tier 1.

## Follow-ups
- B1 parallel sync (Tier 2)
- C1+C2 query API + pi tool (Tier 3)
- A2 materialized summaries (deferred, measure-first)
- B3 decouple pricing from sync (Tier 2)
