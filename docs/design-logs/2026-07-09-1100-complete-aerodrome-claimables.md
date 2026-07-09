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

Derive the complete pool index count from Aerodrome's Factory Registry and each registered factory's `allPoolsLength()`. Cache the count per adapter instance and paginate RewardsSugar through that complete range for every veNFT.

Failure to determine the complete range remains a sync error rather than silently degrading to a partial scan, because a partial result is financially misleading.

## Alternatives Considered

- **Query current voted pools directly** — efficient and fixes the observed pool, but misses outstanding rewards from pools voted in prior epochs.
- **Persist vote history and query known pools** — eventually efficient, but requires schema/backfill work and cannot guarantee immediate completeness.
- **Increase the fixed ceiling** — minimal diff, but repeats the same failure when the pool universe grows again.
- **Do nothing** — rejected because the dashboard materially under-reports claimable income.

## Implementation Notes

- Reuse the Factory Registry and factory `allPoolsLength()` contracts already used by the vote scout.
- Keep `AerodromeAdapter.claimables(venft_id)` unchanged for callers.
- Cache only immutable-within-a-sync metadata; adapter instances are created per command/service sync path.
- Add a regression test with the reward outside the former 3,600-index range.
- The change is read-only onchain and does not claim or move funds.

## Follow-ups

None.
