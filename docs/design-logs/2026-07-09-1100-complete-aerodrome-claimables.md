---
date: 2026-07-09T11:00
type: bugfix
status: proposed
trigger: workaround
touches:
  - hrusha/adapters/aerodrome.py
  - hrusha/service/sync.py
  - tests/test_aerodrome_adapter.py
related:
  - 2026-07-07-1126-vote-scout-probe.md
supersedes: null
superseded_by: null
commit: null
pr: null
---

# Discover all Aerodrome claimables

## Context

Aerodrome voting rewards are exposed by the protocol's RewardsSugar contract over a pool-indexed collection. Hrusha scans that collection for every tracked veNFT and writes positive rewards as claimable snapshots.

The original implementation bounded the scan to 12 calls of 300 pool indexes. Live diagnosis showed that the official Sugar deployment and ABI remained correct, but a voted pool with positive rewards was absent from the bounded global result and present when queried directly.

## Problem / Goal

The registered Aerodrome pool universe outgrew the fixed 3,600-index ceiling. Hrusha could therefore report zero claimable voting rewards even when the protocol reported positive fees and incentives. The dashboard must collect all outstanding rewards, including those left from pools voted in previous epochs.

## Decision

Merge each veNFT's current VeSugar vote pools with every pool previously observed by Hrusha, persist that set in existing `sync_state`, and query each known pool directly with `RewardsSugar.rewardsByAddress()`.

The initially selected registry-complete scan was implemented and passed automated tests, then rejected when live verification exceeded three minutes and saturated the configured RPC. Financial completeness and refresh operability both require targeted reads.

A second design used Blockscout's indexed `Voted` logs for a one-time historical backfill. Live verification rejected it too: the full-history query timed out, Blockscout's JSON-RPC endpoint also stalled, and the configured Alchemy free tier permits `eth_getLogs` ranges of only ten blocks. Historical discovery therefore cannot be a synchronous dashboard dependency. Persisting observed pools makes collection complete from the first successful sync onward; pre-existing, no-longer-current pools require a future explicit backfill using a capable archive/indexing provider.

## Alternatives Considered

- **Blockscout historical `Voted` query** — targeted in theory, but the production-sized query timed out during live verification.
- **Query current voted pools without persistence** — efficient and fixes the observed pool, but forgets pools after the next epoch.
- **Scan the complete registry range** — complete in theory, but live verification exceeded three minutes and saturated the RPC.
- **Increase the fixed ceiling** — minimal diff, but repeats the same correctness failure and retains global-scan cost.
- **Do nothing** — rejected because the dashboard materially under-reports claimable income.

## Implementation Notes

- Store the union of current and previously observed pools in existing `sync_state`; no migration is needed.
- Query the official Sugar `rewardsByAddress(veNFT, pool)` view for deduplicated known pools.
- The change is read-only onchain and does not claim or move funds.

## Follow-ups

Add an explicit historical backfill command if a reliable archive/indexing endpoint becomes available. It must not run in the normal dashboard refresh path.
