---
name: hrusha-run
description: Use when installing, running, or operating hrusha locally — covers venv setup, the CLI command set, and the data-integrity pipeline order. Centers on avoiding wasteful rereads of data: sync is incremental and resumable, prices are cached in SQLite, and the ledger is the single source of truth — do not re-fetch, re-sync, or re-read what is already persisted.
---

# hrusha-run — local install, run, and data-reuse discipline

## When to use

Use this skill when the task involves:
- installing or setting up hrusha for local development
- running any `hrusha` CLI command (`sync`, `balances`, `doctor`, `heal`, `reprice`, `scout`, `invest`, `serve`)
- deciding whether to re-sync, re-price, or re-scan data
- operating the app day-to-day on bare metal

For **deploy / Docker / cloud troubleshooting**, use the sibling `hrusha-ops` skill instead.

## Install (one-time)

```bash
make venv                      # create .venv with python3.12
source .venv/bin/activate
make prepare                   # pip install -e '.[dev]'
make hooks                     # install ruff + gitleaks pre-commit hook — do not skip
```

Config lives in `~/.hrusha/config.yaml` — **created manually, never committed**.
It holds `addresses` (label → address), `alchemy.api_key`, `etherscan.api_key`.
`.gitignore` blocks it; gitleaks scans every change.

A venv is required for every command below. The pre-commit hook
auto-activates `.venv` itself, but the CLI does not — run
`source .venv/bin/activate` first.

## The golden rule: do not reread data that is already persisted

hrusha is built around an **idempotent, resumable, cached** data pipeline.
Wasteful rereads are the #1 way to burn Alchemy/Blockscout quota and your
time. Before re-fetching anything, check what the ledger already holds.

### Sync is incremental and resumable — never restart from block 0

`hrusha sync` tracks a **per-address block cursor** in the SQLite ledger.
Re-running it resumes from the last synced block, not from genesis.
Transfers and fees use a **dedup constraint**, so re-runs and overlaps are
harmless — but there is no reason to trigger them deliberately.

```
hrusha sync --dry-run    # read config, connect to Alchemy, print ETH balances — no writes
hrusha sync              # resume from cursor; transfers, fees, tagging, snapshots -> SQLite
```

- **Do not** drop the DB to "start fresh" unless the ledger is corrupted.
- **Do not** re-sync because you added a tag — tagging is ledger-local; use `hrusha retag`.
- **Do** re-sync only when (a) a new address was added to config, or (b) `doctor` reports a mismatch that `heal` cannot close.

### Prices are cached in SQLite — do not refetch on every run

`PriceResolver` reads from a `price_cache` table (DefiLlama → Alchemy
Prices fallback). Daily prices are cached per (token, day). **NULLs are
cached as definitive misses** (token had no market that day); **transient
API failures are NOT cached** (so a retry can succeed later).

- A normal `sync` prices new events only — it does not refetch existing rows.
- `hrusha reprice` revalues only events still unpriced, and **purges cached NULLs** so genuinely-missed prices get retried.
- **Do not** run `reprice` speculatively — it exists to fix poisoned caches, not to "refresh" prices. Run it only after a provider outage or when a report shows unpriced events.

### The ledger is the single source of truth — read it, don't re-fetch

`balances`, `transfers`, `fees`, `report` all **read from the SQLite
ledger**, not from chain. They are fast and free. Use them to inspect
state. Only `doctor` and `heal` re-read chain, and only to verify the
ledger still matches reality.

```
hrusha balances          # live token balances with USD values (reads cache + one balance call)
hrusha transfers         # recent transfers from the ledger — ids, sources, tags
hrusha fees --days 30    # gas spent over a window (includes Base L1 data fee)
hrusha report --days 90  # neto per epoch x source (--coins for native amounts)
```

### Scout and invest scan are expensive — don't rescan unnecessarily

- `hrusha scout` probes Aerodrome voting pools on-chain (~3.5 min). It is read-only and hits Alchemy hard. **Run it when you want fresh voting suggestions, not on every interaction.** The web dashboard caches the last scan in `ScoutState`; a re-scan replaces it.
- `hrusha invest` fetches APY/TVL from DefiLlama (free, no key) + your balances from Alchemy. Cheap, but still don't loop it — one scan per decision.

## The data-integrity pipeline (run in this order when something looks wrong)

1. **`hrusha sync`** — resume incremental sync. Fixes most "missing data" cases.
2. **`hrusha doctor`** — reconcile ledger vs live on-chain balances.
   - Exit `0`: ledger fully explains on-chain balances. **Stop here.**
   - Exit `5` (`EXIT_RECONCILE_MISMATCH`): ledger is missing inflows/outflows. → proceed to `heal`.
3. **`hrusha heal`** — backfill gaps from Blockscout (dedup-safe). Then **re-run `doctor`** to verify.
4. **`hrusha reprice`** — only if a provider outage left events unpriced, or `report` shows unpriced rows.

Do not run `heal` without first confirming a mismatch via `doctor`.
Do not run `reprice` unless prices are actually missing.

## Running the web dashboard

```bash
hrusha serve              # http://127.0.0.1:8787/  (auto-syncs on a scheduler)
hrusha serve --no-auto-sync   # dashboard only, no background sync loop
```

The scheduler runs `sync` on an interval. If you only want to look at
existing data, use `--no-auto-sync` to avoid wasteful re-syncs while
browsing.

## Exit codes (for scripting and agent decisions)

| code | meaning |
|------|---------|
| 0 | OK |
| 2 | config error (`~/.hrusha/config.yaml` missing or invalid) |
| 3 | upstream provider error (Alchemy/Blockscout/DefiLlama down) |
| 4 | not found |
| 5 | reconcile mismatch (`doctor` found ledger ≠ chain) |

## Agent self-discipline checklist

Before you (the agent) reach for a chain call or a re-sync:
- [ ] Is the answer already in the ledger? Try `balances` / `transfers` / `report` first.
- [ ] Was the data just synced this session? Sync is resumable — don't redo it.
- [ ] Are prices missing, or do you just want "fresher" ones? `reprice` is for missing, not refreshing.
- [ ] Did `doctor` actually report a mismatch before you run `heal`?
- [ ] Is a scan already cached in the dashboard state? Don't re-scan to "make sure."

If unsure whether a re-read is wasteful, ask the operator before burning quota.
