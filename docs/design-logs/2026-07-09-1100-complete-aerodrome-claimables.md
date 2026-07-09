---
date: 2026-07-09T11:00
type: bugfix
status: proposed
trigger: workaround
touches:
  - hrusha/adapters/aerodrome.py
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

Backfill each veNFT's historical pools from the Voter contract's indexed `Voted` events through Blockscout, persist the pool set and cursor in existing `sync_state`, and query each known pool directly with `RewardsSugar.rewardsByAddress()`.

The initially selected registry-complete scan was implemented and passed automated tests, then rejected when live verification exceeded three minutes and saturated the configured RPC. Financial completeness and refresh operability both require targeted reads.

## Alternatives Considered

- **Query current voted pools directly** — efficient and fixes the observed pool, but misses outstanding rewards from pools voted in prior epochs.
- **Scan the complete registry range** — complete in theory, but live verification exceeded three minutes and saturated the RPC.
- **Increase the fixed ceiling** — minimal diff, but repeats the same correctness failure and retains global-scan cost.
- **Do nothing** — rejected because the dashboard materially under-reports claimable income.

## Implementation Notes

- Filter the Voter's `Voted(address,address,uint256,uint256,uint256,uint256)` event by indexed `tokenId` through Blockscout's public logs API.
- Store pool history and a block cursor in existing `sync_state`; no migration is needed.
- Query the official Sugar `rewardsByAddress(veNFT, pool)` view for deduplicated known pools.
- Treat Blockscout's 1,000-result maximum as truncation and fail loudly instead of accepting incomplete history.
- The change is read-only onchain and does not claim or move funds.

## Follow-ups

None.
