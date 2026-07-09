# Complete Aerodrome claimable discovery

## Goal

Make the overview dashboard collect every currently claimable Aerodrome voting reward for each tracked veNFT, including rewards left unclaimed from pools voted in previous epochs.

## Root cause

`AerodromeAdapter.claimables()` currently scans a fixed 12 chunks of 300 pool indexes. Aerodrome's registered pool index space has grown beyond that 3,600-pool ceiling. The bounded global scan therefore returns no reward rows for pools outside the ceiling even though `RewardsSugar.rewardsByAddress()` returns rewards for those pools.

The configured RewardsSugar deployment and tuple ABI match the current official Sugar source. The defect is the local fixed pagination ceiling, not contract selection or ABI decoding.

## Design

The initial registry-complete scan was rejected after live verification exceeded three minutes and saturated the configured RPC. Completeness must not make normal refreshes unusable.

Use targeted pool history instead:

1. Fetch historical `Voted` events from Blockscout, filtering the official Voter contract by the indexed veNFT ID.
2. Merge those pools with the current vote list returned by VeSugar.
3. Persist the deduplicated pool set and backfill cursor in SQLite's existing `sync_state` table.
4. Query `RewardsSugar.rewardsByAddress(veNFT, pool)` once for each known pool.
5. On later syncs, fetch only vote events after the stored cursor, while always merging current votes defensively.

This finds outstanding rewards from current and previous epochs without scanning unrelated pools. No schema migration, configuration change, or new dependency is required.

## Error handling

Vote-history or direct reward-read failures will propagate and fail the Aerodrome snapshot portion of the sync. The history cursor advances only in the same transaction that persists discovered pools. Recording a successful-looking zero when completeness cannot be established would be misleading.

## Testing

Add coverage that:

- parses and filters Blockscout `Voted` logs for one veNFT;
- rejects a 1,000-row response rather than accepting a possibly truncated history;
- persists and incrementally extends per-veNFT pool history;
- queries each known pool exactly once with `rewardsByAddress()`;
- preserves existing fee/bribe and token-decimal behavior.

Run the complete pytest suite plus Ruff lint and formatting checks. After unit verification, perform a read-only live adapter call against the configured Base RPC and confirm that all known live rewards are returned. Then run a normal sync and verify the overview displays non-zero claimables.

## Alternatives rejected

- Query only pools in each veNFT's current vote list: fast, but misses unclaimed rewards from previous epochs.
- Scan the complete Factory Registry range: complete in theory, but live verification exceeded three minutes and saturated the RPC.
- Raise the fixed chunk constant: postpones the same correctness failure and retains global-scan cost.

## Scope

This change affects only Aerodrome claimable discovery and its tests/documentation. It does not claim rewards, change voting, add dependencies, or alter ledger schemas.
