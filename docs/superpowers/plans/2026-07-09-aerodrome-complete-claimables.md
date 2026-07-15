# Aerodrome Complete Claimables Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect every outstanding Aerodrome voting reward by scanning the complete registry-derived pool index range.

**Architecture:** Extend `AerodromeAdapter` with the Factory Registry and minimal factory contracts. Compute and cache the sum of registered factories' `allPoolsLength()` values, then paginate RewardsSugar up to that count instead of a fixed ceiling.

**Tech Stack:** Python 3.12, Web3.py contracts, pytest, Ruff

---

### Task 1: Reproduce rewards beyond the old ceiling

**Files:**
- Modify: `tests/test_aerodrome_adapter.py`

- [ ] **Step 1: Write the failing adapter test**

Import `MagicMock` and `call` from `unittest.mock`, import `AerodromeAdapter`, and add this chain-free test:

```python
def test_claimables_scan_complete_registry_pool_range_and_cache_count():
    token = "0x" + "e" * 40
    factory_a = "0x" + "a" * 40
    factory_b = "0x" + "b" * 40
    zero = "0x" + "0" * 40

    adapter = object.__new__(AerodromeAdapter)
    adapter._w3 = MagicMock()
    adapter._pool_count = None
    adapter._decimals_cache = {token: 18}
    adapter._factory_registry = MagicMock()
    adapter._factory_registry.functions.poolFactories.return_value.call.return_value = [
        factory_a,
        factory_b,
    ]

    contracts = {factory_a: MagicMock(), factory_b: MagicMock()}
    contracts[factory_a].functions.allPoolsLength.return_value.call.return_value = 1_800
    contracts[factory_b].functions.allPoolsLength.return_value.call.return_value = 1_801
    adapter._w3.eth.contract.side_effect = lambda address, abi: contracts[address]

    adapter._rewards_sugar = MagicMock()

    def reward_call(limit, offset, venft_id):
        result = MagicMock()
        result.call.return_value = (
            [(venft_id, OUTSIDER, 25 * 10**17, token, zero, REWARD_CONTRACT)]
            if venft_id == 1 and offset == 3_600
            else []
        )
        return result

    adapter._rewards_sugar.functions.rewards.side_effect = reward_call

    first = adapter.claimables(1)
    second = adapter.claimables(2)

    expected_calls = [
        call(300, offset, venft_id)
        for venft_id in (1, 2)
        for offset in range(0, 3_601, 300)
    ]
    assert adapter._rewards_sugar.functions.rewards.call_args_list == expected_calls
    assert first == [
        Claimable(
            venft_id=1,
            pool=OUTSIDER,
            token=token,
            amount=Decimal("2.5"),
            is_fee=False,
        )
    ]
    assert second == []
    assert adapter._factory_registry.functions.poolFactories.return_value.call.call_count == 1
    assert contracts[factory_a].functions.allPoolsLength.return_value.call.call_count == 1
    assert contracts[factory_b].functions.allPoolsLength.return_value.call.call_count == 1
```

- [ ] **Step 2: Run the regression test and verify RED**

Run:

```bash
.venv/bin/pytest tests/test_aerodrome_adapter.py::test_claimables_scan_complete_registry_pool_range_and_cache_count -v
```

Expected: FAIL because the adapter still stops after offset 3,300 and has no registry-derived count.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_aerodrome_adapter.py
git commit -m "test: reproduce incomplete Aerodrome claimable scan"
```

### Task 2: Derive and cache the complete pool count

**Files:**
- Modify: `hrusha/adapters/aerodrome.py`
- Test: `tests/test_aerodrome_adapter.py`

- [ ] **Step 1: Add the minimal registry ABIs and contract wiring**

Import `AERODROME_FACTORY_REGISTRY`, add private `REGISTRY_ABI` and `FACTORY_ABI` fragments for `poolFactories()` and `allPoolsLength()`, and wire the registry in `AerodromeAdapter.__init__`:

```python
self._factory_registry = w3.eth.contract(
    address=Web3.to_checksum_address(AERODROME_FACTORY_REGISTRY), abi=REGISTRY_ABI
)
self._pool_count: int | None = None
```

- [ ] **Step 2: Add the cached count method**

```python
def _registered_pool_count(self) -> int:
    if self._pool_count is None:
        factories = self._factory_registry.functions.poolFactories().call()
        self._pool_count = sum(
            self._w3.eth.contract(address=factory, abi=FACTORY_ABI)
            .functions.allPoolsLength()
            .call()
            for factory in factories
        )
    return self._pool_count
```

- [ ] **Step 3: Replace fixed chunk iteration**

```python
for offset in range(0, self._registered_pool_count(), POOLS_PER_CALL):
    rewards = self._rewards_sugar.functions.rewards(
        POOLS_PER_CALL, offset, venft_id
    ).call()
```

Remove `MAX_POOL_CHUNKS`; retain existing decoding and decimal handling.

- [ ] **Step 4: Run focused tests and verify GREEN**

```bash
.venv/bin/pytest tests/test_aerodrome_adapter.py -v
```

Expected: all Aerodrome adapter tests pass.

- [ ] **Step 5: Commit implementation**

```bash
git add hrusha/adapters/aerodrome.py tests/test_aerodrome_adapter.py
git commit -m "fix: scan all registered Aerodrome rewards"
```

### Task 3: Verify complete behavior

**Files:**
- Modify: `docs/design-logs/2026-07-09-1100-complete-aerodrome-claimables.md` only if implementation details differ
- Modify: `docs/design-logs/INDEX.md` only if status needs correction before handoff

- [ ] **Step 1: Run the full automated checks**

```bash
.venv/bin/pytest .
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Expected: all tests pass; lint and format checks pass.

- [ ] **Step 2: Run a read-only live adapter probe**

Load the configured adapter, call `claimables()` for each discovered veNFT, and print only veNFT IDs, token symbols, and amounts. Expected: the previously observed FXUSD and USDC rewards are returned instead of empty lists.

```bash
.venv/bin/python - <<'PY'
from web3 import Web3

from hrusha.cli import make_aerodrome_adapter
from hrusha.config import load_config

symbol_abi = [{
    "name": "symbol", "type": "function", "stateMutability": "view",
    "inputs": [], "outputs": [{"name": "", "type": "string"}],
}]
config = load_config()
adapter = make_aerodrome_adapter(config)
for address in config.addresses.values():
    for nft in adapter.venfts(address):
        for reward in adapter.claimables(nft.id):
            token = adapter._w3.eth.contract(
                address=Web3.to_checksum_address(reward.token), abi=symbol_abi
            )
            print(nft.id, token.functions.symbol().call(), reward.amount)
PY
```

- [ ] **Step 3: Run a normal sync and inspect snapshots**

```bash
.venv/bin/hrusha sync
```

Expected: fresh `aerodrome-voting` claimable snapshot rows exist and the dashboard's claimable total is non-zero.

- [ ] **Step 4: Reload and verify the local overview**

Reload `http://127.0.0.1:8787/` and verify the overview reports fresh data and non-zero claimables without exposing secrets.

- [ ] **Step 5: Commit any final documentation adjustment**

If verification required documentation changes:

```bash
git add docs/design-logs/2026-07-09-1100-complete-aerodrome-claimables.md docs/design-logs/INDEX.md
git commit -m "docs: finalize Aerodrome claimable fix record"
```
