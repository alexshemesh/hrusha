# Complete Aerodrome claimable discovery

## Goal

Make the overview dashboard collect Aerodrome voting rewards from every current or previously observed pool for each tracked veNFT, without a global registry scan or a blocking historical-log backfill.

## Root cause

`AerodromeAdapter.claimables()` currently scans a fixed 12 chunks of 300 pool indexes. Aerodrome's registered pool index space has grown beyond that 3,600-pool ceiling. The bounded global scan therefore returns no reward rows for pools outside the ceiling even though `RewardsSugar.rewardsByAddress()` returns rewards for those pools.

The configured RewardsSugar deployment and tuple ABI match the current official Sugar source. The defect is the local fixed pagination ceiling, not contract selection or ABI decoding.

## Design

The initial registry-complete scan was rejected after live verification exceeded three minutes and saturated the configured RPC. Completeness must not make normal refreshes unusable.

Use persisted targeted pools instead:

1. Read the current vote list returned by VeSugar.
2. Merge it with the veNFT's pools persisted by earlier successful syncs.
3. Persist the deduplicated union in SQLite's existing `sync_state` table.
4. Query `RewardsSugar.rewardsByAddress(veNFT, pool)` once for each known pool.

This fixes the observed missing rewards immediately and retains pools across future epochs without scanning unrelated pools. It cannot discover a no-longer-current pool that predates the first successful fixed-version sync; doing that requires an explicit backfill with a historical-log provider capable of serving the Voter's full history. No available endpoint proved capable during live verification, so normal refreshes must not claim that guarantee or block on it. No schema migration, configuration change, or new dependency is required.

## Error handling

Direct reward-read failures propagate and fail the Aerodrome snapshot portion of the sync. Pool persistence occurs in the same transaction as the snapshots. Historical-log availability has no effect on a normal refresh.

## Testing

Add coverage that:

- persists and incrementally extends each veNFT's observed pool set;
- succeeds without invoking the historical vote endpoint;
- queries each known pool exactly once with `rewardsByAddress()`;
- preserves existing fee/bribe and token-decimal behavior.

Run the complete pytest suite plus Ruff lint and formatting checks. After unit verification, perform a read-only live adapter call against the configured Base RPC and confirm that all known live rewards are returned. Then run a normal sync and verify the overview displays non-zero claimables.

## Alternatives rejected

- Blockscout historical `Voted` query: targeted in theory, but timed out against the production history.
- Query only current pools without persistence: fast, but forgets pools after the next epoch.
- Scan the complete Factory Registry range: complete in theory, but live verification exceeded three minutes and saturated the RPC.
- Raise the fixed chunk constant: postpones the same correctness failure and retains global-scan cost.

## Scope

This change affects only Aerodrome claimable discovery and its tests/documentation. It does not claim rewards, change voting, add dependencies, or alter ledger schemas.
