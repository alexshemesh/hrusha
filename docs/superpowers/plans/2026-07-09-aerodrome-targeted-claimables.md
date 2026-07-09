# Aerodrome Targeted Claimables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect Aerodrome claimables with targeted reads over current and previously observed veNFT pools.

**Architecture:** Sync merges current VeSugar vote pools with pools retained in `sync_state`, then asks RewardsSugar directly for each known pool. Historical log backfill stays outside normal refresh because available endpoints could not serve it reliably.

**Tech Stack:** Python 3.12, Web3.py, httpx, SQLite, pytest, Ruff

---

### Task 1: Replace global pagination with direct pool reads

**Files:**
- Modify: `tests/test_aerodrome_adapter.py`
- Modify: `hrusha/adapters/aerodrome.py`

- [x] Write a failing test calling `claimables(venft_id, pools)` with duplicate pools and script `rewardsByAddress()` to return fee and bribe rows.
- [x] Verify RED because the current method accepts no pool list and calls global `rewards()`.
- [x] Add the official `rewardsByAddress(uint256,address)` ABI, deduplicate/lowercase input pools, decode existing `Claimable` values, and remove registry/global pagination code.
- [x] Run `tests/test_aerodrome_adapter.py` and verify GREEN.

### Task 2: Reject blocking historical discovery

**Files:**
- Modify: `tests/test_aerodrome_adapter.py`
- Modify: `hrusha/adapters/aerodrome.py`

- [x] Probe Blockscout, its JSON-RPC endpoint, the configured Alchemy tier, and Base's public RPC.
- [x] Reject synchronous historical discovery after the production-sized queries timed out or required impractical block ranges.
- [x] Keep historical backfill as an explicit future capability requiring a suitable archive/indexing provider.

### Task 3: Persist and incrementally extend observed pools

**Files:**
- Modify: `tests/test_sync.py`
- Modify: `hrusha/service/sync.py`
- Modify: `tests/test_aerodrome_adapter.py` (`StubAdapter` signature only)

- [x] Write a failing sync test with current pool A and stored pool B; assert no historical call occurs and direct claimable reads receive `{A, B}`.
- [x] Verify RED because sync attempted the unavailable historical endpoint.
- [x] Merge/upsert `aero_vote_pools:<id>` JSON and call `claimables(id, pools)`.
- [x] Ensure pool persistence and snapshots commit atomically.
- [x] Run sync and adapter tests and verify GREEN.

### Task 4: Verify and finalize

**Files:**
- Modify: `docs/design-logs/2026-07-09-1100-complete-aerodrome-claimables.md` only if findings differ

- [x] Run `.venv/bin/pytest .`, `.venv/bin/ruff check .`, and `.venv/bin/ruff format --check .`.
- [x] Run a read-only live probe; require the known FXUSD and USDC rewards to appear promptly.
- [x] Run a live dashboard refresh, query fresh `aerodrome-voting` claimable snapshots, and verify non-zero claimables.
- [x] Re-run the design-log trigger checklist and commit final documentation adjustments if any.
