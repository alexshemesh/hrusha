# Aerodrome Targeted Claimables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect complete Aerodrome claimables with targeted reads over persisted veNFT voting history.

**Architecture:** Blockscout supplies historical `Voted` events filtered by indexed veNFT ID. Sync merges those pools with current VeSugar votes, persists pool history/cursor in `sync_state`, and asks RewardsSugar directly for each known pool.

**Tech Stack:** Python 3.12, Web3.py, httpx, SQLite, pytest, Ruff

---

### Task 1: Replace global pagination with direct pool reads

**Files:**
- Modify: `tests/test_aerodrome_adapter.py`
- Modify: `hrusha/adapters/aerodrome.py`

- [ ] Write a failing test calling `claimables(venft_id, pools)` with duplicate pools and script `rewardsByAddress()` to return fee and bribe rows.
- [ ] Verify RED because the current method accepts no pool list and calls global `rewards()`.
- [ ] Add the official `rewardsByAddress(uint256,address)` ABI, deduplicate/lowercase input pools, decode existing `Claimable` values, and remove registry/global pagination code.
- [ ] Run `tests/test_aerodrome_adapter.py` and verify GREEN.

### Task 2: Add Blockscout vote-history discovery

**Files:**
- Modify: `tests/test_aerodrome_adapter.py`
- Modify: `hrusha/adapters/aerodrome.py`

- [ ] Write failing tests using `httpx.MockTransport` for `vote_history(venft_id, from_block)`: decode topic2 pool addresses, filter topic3 token IDs, return the captured head block, accept normal empty results, and reject exactly 1,000 rows as potentially truncated.
- [ ] Verify RED because `vote_history` does not exist.
- [ ] Add an injectable HTTP client, the official `Voted` topic, 32-byte tokenId topic encoding, Blockscout logs request, strict response validation, and `VoteHistory` result type.
- [ ] Run adapter tests and verify GREEN.

### Task 3: Persist and incrementally extend pool history

**Files:**
- Modify: `tests/test_sync.py`
- Modify: `hrusha/service/sync.py`
- Modify: `tests/test_aerodrome_adapter.py` (`StubAdapter` signature only)

- [ ] Write a failing sync test with current pool A, historical pool B, and duplicate pool A; assert one direct claimable call receives `{A, B}` and `sync_state` stores the sorted pool set plus cursor.
- [ ] Verify RED because sync neither fetches nor stores history.
- [ ] Add private sync helpers to read/merge/upsert `aero_vote_pools:<id>` JSON and `aero_vote_cursor:<id>`, then call `claimables(id, pools)`.
- [ ] Ensure cursor and pools commit atomically after successful history discovery.
- [ ] Run sync and adapter tests and verify GREEN.

### Task 4: Verify and finalize

**Files:**
- Modify: `docs/design-logs/2026-07-09-1100-complete-aerodrome-claimables.md` only if findings differ

- [ ] Run `.venv/bin/pytest .`, `.venv/bin/ruff check .`, and `.venv/bin/ruff format --check .`.
- [ ] Run a read-only live probe; require the known FXUSD and USDC rewards to appear promptly.
- [ ] Run `.venv/bin/hrusha sync`, query fresh `aerodrome-voting` claimable snapshots, restart/reload the local dashboard, and verify non-zero claimables.
- [ ] Re-run the design-log trigger checklist and commit final documentation adjustments if any.
