# Aerodrome Vote Discoverability Design

## Goal

Keep Hrusha's vote recommendations aligned with Aerodrome's usable voting UI and describe existing vote allocations accurately across epoch boundaries.

## Observed behavior

Aerodrome's connected vote page defaults to **Most rewarded**. In the live page inspected on 2026-07-09:

- `vAMM-YFI/wstETH` appeared in the default view.
- `CL200-TIG/USDC` and `CL200-MAMO/USDC` appeared only under **All pools** and were labeled **Migrating**.
- The operator's `CL50-FXUSD/USDC` allocation appeared under **All pools** as **Selected to vote**, despite Hrusha labeling every veNFT `NOT voted` after the epoch rollover.

The frontend's current filter excludes pools marked `oldCL` from both the most- and least-rewarded scopes. Aerodrome's protocol documentation also states that an allocation carries forward when it is not manually recast.

## Design

### Factory-aware pool enumeration

Preserve the pool factory while scanning `RewardsSugar.epochsLatest`. The Sugar index space is the concatenation of every registered factory's pool list. Instead of paging across arbitrary global 200-index boundaries, enumerate each factory's global subrange separately. Every returned row can then inherit that factory without an extra RPC call.

The known legacy Slipstream factory is classified as migrating. `RawPool` gains a `migrating` fact, and `score_pool` adds a blocking `MIGRATING` flag. Migrating pools remain visible in **All candidates** for transparency but cannot enter **Suggested**.

This deliberately avoids depending on Aerodrome's private frontend data API and avoids adding one `factory()` RPC call per candidate.

### Vote allocation status

`VeNft` will carry `active_pool_count` from VeSugar's existing `votes` array in addition to `voted_this_epoch`.

The dashboard will render three mutually exclusive statuses:

1. `recast this epoch ✓` when `voted_at` is in the running epoch;
2. `carried forward — N pools` when an older vote still has active pool allocations;
3. `no active allocation` when the veNFT has no current vote entries.

This separates the transaction recency question from the allocation state.

### Discoverability guidance

The page will link to Aerodrome's vote screen and state that pools outside its default **Most rewarded** scope can be found through **Pools → All pools**. Pool names keep their block-explorer links because Aerodrome does not expose a stable direct pool-search URL.

## Error handling and trust boundaries

- Factory identities are public on-chain protocol addresses and belong in `known_contracts.py`.
- Unknown factories are not assumed to be migrating; the existing risk gates continue to decide their eligibility.
- A future Aerodrome migration requires adding the newly retired factory to the explicit legacy set. The design favors an auditable allowlist over inferring protocol policy from contract age or ordering.
- No wallet action, vote transaction, or signature is added. The feature remains read-only.

## Testing

- Pure scoring test: a migrating pool receives `MIGRATING` and is not suggested.
- Enumeration test: factory-local pages use correct global offsets and retain the source factory, including a factory boundary.
- View-model test: VeSugar vote entries populate `active_pool_count`.
- Dashboard tests cover all three allocation labels and the Aerodrome **All pools** guidance.
- Full pytest, Ruff, and formatting verification run before completion.

## Alternatives considered

- **Aerodrome frontend API**: rejected because it is not a documented public contract and would add a new availability dependency to a weekly on-chain scan.
- **Call `factory()` for every candidate**: correct but rejected because the scan is already RPC-latency bound.
- **Only add UI instructions**: rejected because migrating pools would still be presented as financially clean suggestions.

