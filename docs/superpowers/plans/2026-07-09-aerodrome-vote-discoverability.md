# Aerodrome Vote Discoverability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent migrating Aerodrome pools from being recommended, accurately describe carried-forward vote allocations, and tell operators how to find pools outside Aerodrome's default view.

**Architecture:** The scanner will preserve each pool's source factory by paging Sugar within factory boundaries, allowing migration status to remain an on-chain fact without extra per-pool calls. Pure scoring turns that fact into a blocking flag. VeSugar's existing vote entries provide allocation state independently from the current-epoch transaction timestamp, and the server-rendered page explains both states.

**Tech Stack:** Python 3.12, Web3.py, FastAPI/Jinja, pytest, Ruff

---

### Task 1: Exclude migrating pools without extra RPC calls

**Files:**
- Modify: `hrusha/adapters/known_contracts.py`
- Modify: `hrusha/service/vote_scout.py`
- Test: `tests/test_vote_scout.py`

- [ ] **Step 1: Write failing scoring and pagination tests**

Add the imports and tests below to `tests/test_vote_scout.py`:

```python
from hrusha.service.vote_scout import (
    RawPool,
    ScoutResult,
    _factory_pages,
    rank,
    score_pool,
)


def test_migrating_pool_is_flagged_and_never_suggested():
    score = score_pool(clean_pool(migrating=True), AERO_PRICE, MY_POWER)
    assert "MIGRATING" in score.flags
    assert not score.suggested


def test_factory_pages_preserve_factory_boundaries_and_global_offsets():
    pages = list(_factory_pages([("old", 250), ("new", 30)]))
    assert pages == [
        ("old", 200, 0),
        ("old", 50, 200),
        ("new", 30, 250),
    ]
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_vote_scout.py::test_migrating_pool_is_flagged_and_never_suggested tests/test_vote_scout.py::test_factory_pages_preserve_factory_boundaries_and_global_offsets -q
```

Expected: collection fails because `_factory_pages` does not exist and `RawPool` does not accept `migrating`.

- [ ] **Step 3: Implement factory-aware enumeration and the blocking flag**

In `hrusha/adapters/known_contracts.py`, add the public legacy factory identity:

```python
LEGACY_SLIPSTREAM_POOL_FACTORIES = frozenset({LEGACY_SLIPSTREAM_POOL_FACTORY})
```

Define `LEGACY_SLIPSTREAM_POOL_FACTORY` immediately above it using the verified public
factory address from the 2026-07-09 live comparison. The literal belongs only in
`known_contracts.py`, the repository's gitleaks-allowlisted public-contract registry.

In `hrusha/service/vote_scout.py`:

```python
from hrusha.adapters.known_contracts import LEGACY_SLIPSTREAM_POOL_FACTORIES


def _factory_pages(
    factories: list[tuple[str, int]], page_size: int = POOL_INDEXES_PER_CALL
):
    global_offset = 0
    for factory, pool_count in factories:
        for local_offset in range(0, pool_count, page_size):
            yield factory, min(page_size, pool_count - local_offset), global_offset + local_offset
        global_offset += pool_count
```

Add `migrating: bool = False` to `RawPool`. Add `MIGRATING` to `score_pool` when true. Replace the aggregate factory-length scan with `(factory, length)` pairs and iterate `_factory_pages`; attach this fact to every row:

```python
factory_lengths = [
    (
        factory.lower(),
        w3.eth.contract(address=factory, abi=FACTORY_ABI).functions.allPoolsLength().call(),
    )
    for factory in registry.functions.poolFactories().call()
]
pool_count = sum(length for _factory, length in factory_lengths)

for factory, limit, offset in _factory_pages(factory_lengths):
    rows = rewards_sugar.functions.epochsLatest(limit, offset).call()
    for ts, lp, votes, emissions, bribes, fees in rows:
        if ts != epoch_start:
            raise RuntimeError(f"LpEpoch decode looks wrong: ts={ts} != {epoch_start}")
        if lp.lower() in seen:
            continue
        seen.add(lp.lower())
        pools.append(
            {
                "lp": lp,
                "votes": votes / WEI,
                "bribes": bribes,
                "fees": fees,
                "emissions_rate": emissions / WEI,
                "migrating": factory in LEGACY_SLIPSTREAM_POOL_FACTORIES,
            }
        )
```

Pass the migration fact into both `RawPool` construction paths:

```python
migrating=p["migrating"],
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_vote_scout.py -q
```

Expected: all vote-scout unit tests pass.

- [ ] **Step 5: Commit the migration gate**

```bash
git add hrusha/adapters/known_contracts.py hrusha/service/vote_scout.py tests/test_vote_scout.py
git commit -m "fix: exclude migrating Aerodrome pools"
```

### Task 2: Distinguish carried-forward allocations from recasts

**Files:**
- Modify: `hrusha/service/vote_scout.py`
- Modify: `hrusha/service/templates/votes.html`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write a failing dashboard status test**

Add to `tests/test_app.py`:

```python
def test_votes_page_distinguishes_recast_carried_and_empty_allocations(config):
    from hrusha.service.vote_scout import VeNft

    result = make_scout_result()
    result.venfts = [
        VeNft(1, 100.0, True, "main", active_pool_count=1),
        VeNft(2, 200.0, False, "main", active_pool_count=2),
        VeNft(3, 300.0, False, "main", active_pool_count=0),
    ]
    client = make_client(config, scout_runner=lambda cfg: result)
    client.post("/votes/scan", follow_redirects=False)
    for _ in range(100):
        body = client.get("/votes").text
        if "veNFT #3" in body:
            break
        time.sleep(0.02)
    assert "recast this epoch ✓" in body
    assert "carried forward — 2 pools" in body
    assert "no active allocation" in body
    assert "NOT voted" not in body
```

- [ ] **Step 2: Run the dashboard test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_app.py::test_votes_page_distinguishes_recast_carried_and_empty_allocations -q
```

Expected: failure because `VeNft` has no `active_pool_count` and the old label is rendered.

- [ ] **Step 3: Implement the allocation state**

Extend `VeNft` without breaking existing fixtures:

```python
@dataclass
class VeNft:
    id: int
    power: float
    voted_this_epoch: bool
    wallet_label: str
    active_pool_count: int = 0
```

In `_fetch_venfts`, set `active_pool_count=len(nft["votes"])`. Replace the template label with:

```jinja2
{% if nft.voted_this_epoch %}recast this epoch ✓
{% elif nft.active_pool_count %}carried forward — {{ nft.active_pool_count }}
  pool{{ '' if nft.active_pool_count == 1 else 's' }}
{% else %}no active allocation{% endif %}
```

Update the existing dashboard assertion from `NOT voted` to `no active allocation`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_app.py -q
```

Expected: all dashboard tests pass.

- [ ] **Step 5: Commit allocation semantics**

```bash
git add hrusha/service/vote_scout.py hrusha/service/templates/votes.html tests/test_app.py
git commit -m "fix: show carried Aerodrome vote allocations"
```

### Task 3: Add Aerodrome discoverability guidance

**Files:**
- Modify: `hrusha/service/templates/votes.html`
- Test: `tests/test_app.py`

- [ ] **Step 1: Write failing guidance assertions**

In `test_votes_scan_renders_suggestions_in_percent_and_flags_traps`, add:

```python
assert 'href="https://aerodrome.finance/vote?"' in body
assert "Pools → All pools" in body
assert "migrating pools are excluded" in body
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_app.py::test_votes_scan_renders_suggestions_in_percent_and_flags_traps -q
```

Expected: failure because the Aerodrome link and instructions are absent.

- [ ] **Step 3: Add read-only navigation guidance**

Below the suggested-pools heading in `votes.html`, add:

```jinja2
<p class="muted">
  Open <a href="https://aerodrome.finance/vote?">Aerodrome voting</a>.
  Its default view is <b>Most rewarded</b>; use <b>Pools → All pools</b> to find
  lower-reward pools. Aerodrome migrating pools are excluded from suggestions here.
</p>
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
.venv/bin/pytest tests/test_app.py::test_votes_scan_renders_suggestions_in_percent_and_flags_traps -q
```

Expected: pass.

- [ ] **Step 5: Commit discoverability guidance**

```bash
git add hrusha/service/templates/votes.html tests/test_app.py
git commit -m "feat: link vote suggestions to Aerodrome"
```

### Task 4: Full verification and release gate

**Files:**
- Modify if necessary: `docs/design-logs/2026-07-09-1220-vote-discoverability.md`

- [ ] **Step 1: Run the complete test suite**

```bash
.venv/bin/pytest -q > /tmp/hrusha-vote-tests.log 2>&1
tail -20 /tmp/hrusha-vote-tests.log
```

Expected: zero failures.

- [ ] **Step 2: Run lint and formatting checks**

```bash
.venv/bin/ruff check . > /tmp/hrusha-vote-ruff.log 2>&1
.venv/bin/ruff format --check . > /tmp/hrusha-vote-format.log 2>&1
tail -20 /tmp/hrusha-vote-ruff.log
tail -20 /tmp/hrusha-vote-format.log
```

Expected: both commands exit zero.

- [ ] **Step 3: Re-run the design-log trigger checklist**

Confirm the proposed entry covers the migration workaround, financial-data integrity, touched files, alternatives, and follow-ups. Keep status `proposed` until merge.

- [ ] **Step 4: Inspect the final diff and worktree**

```bash
git diff main --check
git status --short --branch
git log --oneline --decorate -6
```

Expected: clean diff check; only intentional committed changes on `fix/aerodrome-vote-discoverability`.
