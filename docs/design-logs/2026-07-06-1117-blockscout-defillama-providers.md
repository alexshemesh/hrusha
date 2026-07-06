---
date: 2026-07-06T11:17
type: architecture
status: proposed
trigger: supersession
touches:
  - hrusha/providers/blockscout.py
  - hrusha/providers/interface.py
  - hrusha/prices.py
  - hrusha/service/sync.py
  - hrusha/cli.py
  - docs/examples/fetch_wallet_data.py
related:
  - 2026-07-05-0842-repurpose-eth-portfolio-monitor.md
supersedes: null
commit: null
pr: null
---

# Source transfers from Blockscout and prices from DefiLlama

## Context

docs/DESIGN.md chose Alchemy (free tier) as the primary data provider —
balances, transfer history, receipts, historical prices — with CoinGecko
as the price fallback and Etherscan for fee accounting. Phase 1 was
implemented on that plan. During live verification against a real
wallet (~2,100 transfers, 206 distinct tokens, history from 2025-04),
three of those choices failed in ways no code change could work around.

## Problem / Goal

- Alchemy's Transfers API (`alchemy_getAssetTransfers`) was load-shed
  globally for free-tier apps for over 24 hours ("rate-limited due to
  unusually high global traffic") while the same key served plain
  JSON-RPC fine. The app dashboard showed 0% throughput limited — the
  block happens at their edge and is invisible per-app. Not fixable by
  retries, new apps, or backoff.
- Etherscan v2 dropped Base from its free tier entirely ("Free API
  access is not supported for this chain").
- CoinGecko's keyless API rejects history older than 365 days, so it
  cannot price the first months of the wallet's history at all; it also
  rate-limits hard under a backfill's request volume.
- Pricing per (token, day) needed 643 requests for one wallet; combined
  with per-request retry backoff against throttled providers, a full
  sync took hours or failed.

## Decision

- **Transfers: Blockscout** (base.blockscout.com, Etherscan-compatible
  API, free, no key — nothing to leak). `txlist` for top-level ETH
  transfers, `tokentx` for ERC-20. New `BlockscoutProvider` behind a
  narrow `TransferSource` protocol; sync takes it as a separate
  dependency next to the main DataProvider.
- **Prices: DefiLlama primary** (coins.llama.fi, free, no key). On the
  first cache miss for a token, one `/chart` call fetches the token's
  entire daily history and fills the SQLite price cache — ~1 request
  per distinct token instead of one per (token, day). Unlimited history
  depth, prices from on-chain DEX pools, unknown (spam) tokens are
  silently omitted which maps cleanly to a definitive, cacheable miss.
- **Alchemy stays** for what its free tier serves reliably: Portfolio
  balances, batched receipts (exact fees incl. Base L1 data fee), and
  as a per-day price fallback behind a circuit breaker (3 consecutive
  failures → skipped for the rest of the run).
- **CoinGecko removed; Etherscan unused** (key kept in config, harmless).
- Caching rule that fell out of the live failure: only definitive
  misses are cached as NULL; transient failures (429/5xx/timeouts) are
  never cached, otherwise a throttled backfill poisons the price cache
  for legit tokens.

## Alternatives Considered

- **Wait out / upgrade Alchemy** — throttling lasted 24h+ with no ETA;
  paying to work around a prototype's data source contradicts the
  free-tier goal.
- **New Alchemy app / key** — the block proved to be tier-wide at their
  edge, not per-app.
- **Etherscan v2 for transfers** — no free Base access anymore.
- **CoinGecko with demo key** — still capped at 365 days of history.
- **GeckoTerminal / CryptoCompare / Moralis for prices** — thin
  long-tail Base coverage or CU-metered free tiers; DefiLlama covered
  every live-probed case (old AERO price, ETH, spam token) keylessly.
- **Migrating to Go for parallel requests** — measured the workload
  first: 24 requests fetch everything except prices, and the wall time
  is rate limits plus retry backoff, which parallelism makes worse. The
  fix was request *shape* (batch price history per token), not speed.

## Implementation Notes

- Blockscout `tokentx` returns `logIndex: null` → synthetic per-tx
  ordinals (0, 1, ...) as log_index. Stable within one source, NOT
  comparable to real log indexes: mixing Blockscout-ingested history
  with Alchemy-ingested history in one ledger would duplicate events.
- Blockscout does not filter spam tokens; symbols are attacker-
  controlled (homoglyph/RTL phishing text) and are sanitized (printable
  chars only, capped at 32). Contract address remains the token identity.
- Empty results arrive as HTTP 200, status "0", message "No
  transactions found" / "No token transfers found" — matched by shape
  ("No ... found"), not exact text.
- DefiLlama `/chart` span is capped (~500 days); the resolver refetches
  with an earlier start if another wallet sees a token before the
  covered range.
- Live verification: full backfill 2,113 transfers / 170 fees / 97
  snapshots in ~8.5 min; re-sync ingests 0; fees match the Blockscout
  explorer to 12 decimal places.

## Follow-ups

- docs/DESIGN.md still describes the superseded provider matrix; update
  its provider section.
- Gas of reverted txs and zero-value contract calls (approves, votes)
  is not yet counted — txlist exposes them, so fee coverage can improve.
- Spam-token events are ingested (contract is the identity); Phase 2
  tagging should let reports exclude them.
- A price backfill command for events that stayed unpriced after
  transient failures (risk register #2).
