# Complete Aerodrome claimable discovery

## Goal

Make the overview dashboard collect every currently claimable Aerodrome voting reward for each tracked veNFT, including rewards left unclaimed from pools voted in previous epochs.

## Root cause

`AerodromeAdapter.claimables()` currently scans a fixed 12 chunks of 300 pool indexes. Aerodrome's registered pool index space has grown beyond that 3,600-pool ceiling. The bounded global scan therefore returns no reward rows for pools outside the ceiling even though `RewardsSugar.rewardsByAddress()` returns rewards for those pools.

The configured RewardsSugar deployment and tuple ABI match the current official Sugar source. The defect is the local fixed pagination ceiling, not contract selection or ABI decoding.

## Design

Keep `AerodromeAdapter.claimables(venft_id)` as the public interface and retain `RewardsSugar.rewards()` as the discovery mechanism. Before scanning, the adapter will:

1. Read all pool factories from Aerodrome's Factory Registry.
2. Sum `allPoolsLength()` across those factories to obtain the authoritative pool index count used by Sugar.
3. Cache that count for the adapter lifetime so all veNFTs in one sync share the same metadata reads.
4. Iterate offsets from zero through the complete count in `POOLS_PER_CALL` chunks.

This preserves discovery of outstanding rewards from prior voting epochs. No database or configuration changes are required.

## Error handling

Registry or factory RPC failures will propagate and fail the Aerodrome snapshot portion of the sync. Recording a successful-looking zero when completeness cannot be established would be misleading. Existing service-level handling will report the sync failure without crashing the dashboard.

## Testing

Add adapter-level regression coverage with mocked Web3 contracts that:

- report a pool count beyond 3,600;
- return a positive reward only in a later chunk;
- verify the later chunk is queried and decoded;
- verify the registry-derived count is cached across multiple veNFT calls;
- preserve existing fee/bribe and token-decimal behavior.

Run the complete pytest suite plus Ruff lint and formatting checks. After unit verification, perform a read-only live adapter call against the configured Base RPC and confirm that all known live rewards are returned. Then run a normal sync and verify the overview displays non-zero claimables.

## Alternatives rejected

- Query only pools in each veNFT's current vote list: fast, but misses unclaimed rewards from previous epochs.
- Persist vote history and query every stored pool directly: adds schema and backfill complexity and cannot immediately recover pools missing from local history.
- Raise the fixed chunk constant: postpones the same failure and does not establish completeness.

## Scope

This change affects only Aerodrome claimable discovery and its tests/documentation. It does not claim rewards, change voting, add dependencies, or alter ledger schemas.
