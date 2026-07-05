# hrusha — Implementation Plan

Companion to [DESIGN.md](DESIGN.md) (v2). Phases are ordered by
value-first / risk-early: real numbers on screen after Phase 1, the
riskiest integration (Aerodrome claimables) immediately after the
foundations, cosmetics last.

Sizes: **S** ≈ an evening, **M** ≈ 2–3 evenings, **L** ≈ a week of
evenings. Every phase ends in something usable.

---

## Phase 0 — Repo reset & skeleton (S)

Clear out the legacy and put up the frame.

- Move legacy Bitfinex code (`bitfinex.py`, `strategy*.py`,
  `fake_processor.py`, `test.py` + their tests) to `legacy/` — delete in a
  later phase once nothing is missed.
- New package layout per DESIGN.md: `service/`, `providers/`, `adapters/`,
  `ledger/`, `export/`, `web/`.
- Config loader for `~/.hrusha/config.yaml` (PyYAML — stdlib has no YAML
  parser): `addresses` (2–4, labeled), `alchemy`, `etherscan`, `sheets`
  (later). The owner creates this file manually; it lives outside the repo
  and is never committed. Fail with a clear message when missing or
  malformed — name the missing key, never echo values.
- SQLite schema v1 + migrations-by-version: `events`, `snapshots`, `tags`,
  `tag_rules`, `epochs`, `sync_state`.
- Modernize tooling: Python 3.12, `ruff` (replaces pycodestyle), fix the
  workflow filename (`tets.yml` → `test.yml`), pin dependencies.
- **Public-repo guardrails from day one** (before any real data exists):
  hardened `.gitignore`/`.dockerignore` (`config.yaml`, `*.db*`, `.env*`,
  `*service-account*.json`, `/data`), `gitleaks` pre-commit hook + CI job,
  GitHub secret scanning & push protection enabled on the repo.
- Dockerfile skeleton (multi-stage, `python:3.12-slim`, non-root) so the
  container path is exercised from the start, even before it's published.

**Done when:** `hrusha sync --dry-run` reads the config, connects to
Alchemy, and prints the ETH balance of each configured address — both
natively and via `docker run` with `/config` mounted; and a deliberately
planted fake secret is blocked by the pre-commit hook.

## Phase 1 — Ledger + Alchemy sync on Base (M)

The core loop: chain → SQLite → report.

- `providers/interface.py`: `DataProvider` protocol — `balances()`,
  `transfers(since_block)`, `prices()`, `positions()`, `claimables()` (the
  last two raise `NotImplemented` here; adapters fill them, DeBank later).
- `providers/alchemy_rpc.py`:
  - Portfolio API → token balances + USD prices per address (one call).
  - `getAssetTransfers` → incremental in/out transfer history per address
    (cursor in `sync_state`, idempotent re-runs).
  - Receipts for every outgoing tx → `gas_fee` events
    (`gasUsed × effectiveGasPrice`), USD-priced at tx time.
- Event ingestion with dedup (unique on `tx_hash + log_index + kind`).
- Own-transfer detection: transfers between the 2–4 tracked addresses
  auto-tagged `own-transfer`, excluded from income/spend.
- Price fallback for long-tail tokens: CoinGecko (bribe tokens may be
  missing from Alchemy prices — expect this).
- CLI reports: `hrusha balances`, `hrusha transfers`, `hrusha fees` — all
  USD + native amounts.

**Done when:** a full sync of your real addresses is repeatable
(idempotent), and `hrusha fees` matches BaseScan for a few spot-checked
transactions.

## Phase 2 — Tagging engine + epoch calendar (S/M)

The reporting backbone; adapters plug into it next.

- `tag_rules`: ordered rules (match on counterparty address, token, kind,
  direction) → tags + source. Seed rules: known Aerodrome/Morpho/40acres
  contract addresses, `own-transfer`, CEX deposit addresses (tag now, code
  later).
- `reinvest` heuristic: swap from a tracked address within N hours after a
  claim → tag `reinvest`.
- Manual overrides: `hrusha tag <event-id> <tag>` (UI editing in Phase 5);
  manual always wins over rules; re-running rules never clobbers manual.
- Aerodrome epoch calendar (weekly flip, Thu 00:00 UTC): every event gets
  an `epoch_id`; `hrusha report --by-epoch --by-source`.

**Done when:** the neto report exists and is honest for the data we have so
far (transfers + fees), grouped by epoch × source.

## Phase 3 — Aerodrome adapter (L) — the main income source

**Spike first (S, do before committing to the phase):** verify on your real
address that Aerodrome's Sugar helper contracts (or direct
Voter/Gauge/Bribe reads) expose: veNFT ids + lock size, votes cast this
epoch, and **claimable bribes + fees per gauge**. This is the plan's
biggest unknown; if Sugar falls short, fallback is enumerating gauges voted
on and calling their reward contracts directly.

Then:

- Discover veNFTs owned by tracked addresses; lock amount/expiry; votes
  cast in current epoch (dashboard needs "have I voted yet" state).
- Claimables per gauge, USD + native (the night-before-epoch view).
- Historical claims from event logs → `reward_claim` events, source
  `aerodrome-voting`, USD at claim time.
- `vote` events recorded for the audit trail.

**Done when:** the per-epoch neto for `aerodrome-voting` matches what you
believe you earned in the last 2–3 epochs (manual reconciliation with
your own records/BaseScan).

## Phase 4 — Morpho + 40acres adapters (M)

- `adapters/morpho.py`: positions per address via Morpho's public GraphQL
  API (fallback: vault contract reads) — supplied principal, current value,
  accrued yield. Deposits/withdrawals as ledger events; yield accrual as
  periodic `reward_claim`-style income events, source `morpho`.
- `adapters/forty_acres.py`: **supply side** — supplied principal + accrued
  interest, source `40acres`. Expect a mini-spike: small protocol, likely
  contract reads from ABIs pulled off BaseScan verified sources rather than
  docs.

**Done when:** all three v1 sources appear in `hrusha report` with correct
principal and income, verified against the protocols' own UIs.

## Phase 5 — Service + web dashboard (L)

- `service/scheduler.py`: hourly sync; every 10–15 min in the 6 hours
  around epoch flip; jittered retries; failures logged, never crash the
  service.
- `service/app.py` (FastAPI) + minimal server-rendered pages (no SPA):
  - **Overview**: balances per address, total; claimables per source;
    epoch countdown; voted-this-epoch indicator.
  - **Income**: neto per source per epoch (USD + coins), drill-down to
    ledger rows.
  - **Transfers & Fees** views with tag filters.
  - **Alerts panel**: large/unexpected transfer, gas-spike warning
    (current gas vs trailing average).
  - Manual Refresh button; inline tag editing.
- Runs natively during development; production runs in Docker via
  Phase 6's compose setup.

**Done when:** the service survives a week unattended (dev machine is fine
at this phase), including one epoch flip, with correct data and no manual
restarts.

## Phase 6 — Packaging, CI/CD & deployment (M)

Turns the service into something the *other* machine runs.

- GitHub Actions:
  - PR/push: `ruff` + full test suite + `gitleaks` (no secrets exist in
    CI — tests run on fixtures only).
  - On version tag (`v*`): build multi-arch image (amd64 + arm64, in case
    the home machine is a Pi/ARM box) and push to
    `ghcr.io/<owner>/hrusha` with semver + `latest` tags, using the
    built-in `GITHUB_TOKEN`.
- `compose.yaml`: service + `/data` and read-only `/config` mounts,
  `restart: unless-stopped`, healthcheck, dashboard bound to `127.0.0.1`
  by default.
- `deploy/install.sh` for a fresh target machine: create dirs, prompt for
  config location, pull image, install systemd unit (or launchd plist)
  wrapping `docker compose up -d`. `deploy/upgrade.sh`: pull tag, backup
  DB, `compose up -d`.
- DB ops commands shipped: `hrusha db backup|restore|check`; automatic
  pre-migration backup; nightly backup in the scheduler; 30-day rotation.
- `docs/OPERATIONS.md`: install, upgrade, rollback, backup/restore,
  moving machines, "dashboard must never face the internet".

**Done when:** a fresh machine (or clean VM) goes from zero to a running,
synced service using only the public repo's instructions + your private
`config.yaml` — and the image on ghcr.io contains no trace of config/data.

## Phase 7 — Google Sheets export (S/M)

- Service-account auth; sheet shared to the SA email; id in config.
- **Ledger tab**: append-only, one row per event (row key = event id, so
  re-export never duplicates).
- **Epoch summary tab**: one row per epoch × source — gross, gas, neto in
  USD; native coin totals.
- Nightly export in the scheduler + `hrusha export` for on-demand.

**Done when:** you can build a pivot in Sheets answering "neto per source
per month" without touching SQLite.

## Phase 8 — Hardening (S, ongoing)

- Structured logs (addresses stay in local logs only, never shipped);
  last-sync status surfaced in the dashboard.
- Spot-check command: `hrusha verify` re-derives a random sample of events
  from Etherscan V2 (Base) and diffs against the ledger.
- Dependabot for pip + Docker base image + Actions versions.

---

## Testing strategy (applies to every phase)

- **Unit tests** per module; ledger/report logic tested against a golden
  synthetic ledger (known events in → exact neto per source per epoch out).
- **Adapter/provider tests on recorded fixtures**: RPC and API responses
  captured once from live calls, then **scrubbed** — real addresses/tx
  hashes replaced with synthetic ones before committing (the repo is
  public; fixtures are the most likely leak vector).
- **CI runs everything with zero secrets.** Anything needing live keys is
  a separate local-only command (`hrusha smoke`), never a test.
- **Migration tests**: every schema migration gets a test applying it to
  the previous version's fixture DB.
- Coverage tracked in CI; no phase is "done" with its tests missing.

---

## Explicitly deferred (designed-for, not built)

| Item | Hook already in place |
|---|---|
| Velodrome/Optimism + more ve(3,3) networks | same adapter shape, chain field everywhere |
| DeBank provider (~$100 tier) | `DataProvider` interface |
| Binance/Bitfinex read-only ingestion | `source`/tags model, ledger schema |
| Telegram/email alert delivery | alerts computed in Phase 5, delivery is a sink |
| Auto-claiming of voting rewards | separate module + security review (signing keys) |

## Risk register

1. **Aerodrome claimables readability** — mitigated by the Phase 3 spike
   *before* building the full adapter.
2. **Long-tail bribe-token prices** missing from Alchemy — CoinGecko
   fallback in Phase 1; worst case, price-at-claim backfilled later.
3. **40acres has no docs/API** — ABIs from verified contracts on BaseScan;
   time-boxed mini-spike, and DeBank (later) likely covers it anyway.
4. **Historical backfill depth** — `getAssetTransfers` covers full history,
   but USD-at-time for old events needs historical prices (CoinGecko free
   tier is daily-granularity; acceptable).

## Suggested first session

Phase 0 + start of Phase 1: repo reset, config, schema, and a first live
`hrusha sync --dry-run` against your real addresses (keys and addresses go
only into `~/.hrusha/config.yaml`, which you create manually).
