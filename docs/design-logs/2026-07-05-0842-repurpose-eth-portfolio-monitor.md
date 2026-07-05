---
date: 2026-07-05T08:42
type: architecture
status: merged
trigger: architecture
touches:
  - README.md
  - docs/DESIGN.md
related: []
supersedes: null
commit: null
pr: null
---

# Repurpose hrusha as an ETH portfolio monitor on Alchemy + Etherscan free tiers

## Context

hrusha was a hobby-stage Bitfinex trading bot (paper-trading simulator,
ladder strategy, read-only exchange client). The owner's actual need is
different: monitoring digital assets across several personal Ethereum
addresses — balances, in/out transfers, gas fees, and profits. The DeBank
app is the UX benchmark for what "good" looks like.

## Problem / Goal

On-chain data is public but raw: enumerating ERC-20 holdings requires
per-token contract calls, there are thousands of tokens plus spam, and USD
prices are not on-chain. A data provider is needed. Selection criteria set
by the owner: **fastest and cheapest**, for a single-user tool.

## Decision

- Repurpose the project; the Bitfinex code becomes legacy.
- **Primary provider: Alchemy free tier** (30M CU/month) — Portfolio API
  returns all token balances with USD prices in one call per address;
  `alchemy_getAssetTransfers` covers in/out transfer history.
- **Secondary: Etherscan API V2 free tier** (5 req/s, 100k calls/day) —
  `txlist` gives `gasUsed × gasPrice` per transaction for exact fee
  accounting, plus an independent cross-check of transfers.
- **PnL computed locally** (SQLite cost-basis lots from transfer history +
  prices), since no free tier provides it directly.
- Total running cost: $0/month at personal scale.

### Refined in same-day design session (see DESIGN.md v2)

- Scope sharpened from generic "ETH portfolio monitor" to **crypto income
  monitor**: neto per source (Aerodrome ve(3,3) voting, Morpho vaults,
  later CEX rewards), fees, and money flows.
- **v1 networks: Base only.** Ethereum mainnet dropped from v1.
- **DeFi positions via protocol adapters over Alchemy RPC** (Aerodrome
  veNFT/gauges/bribes, Morpho vaults), isolated behind a `DataProvider`
  interface because the owner intends to **upgrade to DeBank's paid tier
  (~$100)** later — owner's prior experience: DeBank pays off and bundles
  prices with positions. This softens v1's "rejected on cost" verdict for
  DeBank from rejection to deferral.
- Runs as a **home-server service**: hourly sync (denser near the weekly
  epoch flip), local web dashboard (FastAPI), and append-only **Google
  Sheets export** (ledger tab + per-epoch summary tab).
- **Ledger events carry USD-at-event-time AND native coin amounts**, plus
  a tag system (source/counterparty/purpose) designed to absorb future
  Binance/Bitfinex data without schema changes.
- v1 alerts: large/unexpected transfers, gas-spike warning (no third —
  epoch reminders live in the owner's Google Calendar).
- v1 is strictly read-only; future auto-claiming of voting rewards is
  deferred to a separately-reviewed module (signing keys involved).
- 40acres.finance identified as a standalone Base protocol (not a Morpho
  vault); owner is on the supply side — third adapter alongside
  Aerodrome and Morpho.

### Packaging & public-repo decisions (same-day, later session)

- **Public GitHub repo** with dev on the owner's Mac and production on a
  different home machine. Distribution: **Docker image on ghcr.io** built
  by GitHub Actions on version tags; `compose.yaml` + install/upgrade
  scripts on the target.
- **Security is paramount** (public repo, private finances): config/data
  only in mounted volumes, hardened `.gitignore`/`.dockerignore`,
  `gitleaks` pre-commit + CI, GitHub push protection, zero secrets in CI
  (tests run on scrubbed recorded fixtures), dashboard bound to
  localhost/LAN only. Accepted residual: the repo reveals the *strategy*,
  never identities/addresses/amounts.
- SQLite treated as derived state in a `/data` volume: forward-only
  auto-migrations with pre-migration backup, nightly backups + 30-day
  rotation, `db backup|restore|check` commands, documented in
  `docs/OPERATIONS.md`.
- Alternatives considered: bare-metal install with venv + systemd on the
  target (rejected: dependency drift between machines, no clean upgrade/
  rollback); publishing to Docker Hub (rejected: ghcr keeps auth and CI
  in one place with `GITHUB_TOKEN`); PostgreSQL container (rejected:
  overkill for a single-user append-mostly ledger; SQLite + backups is
  simpler to operate at home).

## Alternatives Considered

- **DeBank OpenAPI (Cloud)** — richest data (exactly what the DeBank app
  shows, incl. DeFi positions), but paid-only prepaid units with no free
  tier; rejected on cost for a personal tool. Revisit if DeFi position
  tracking becomes a must-have.
- **MetaMask / Infura** — MetaMask has no public portfolio API; Infura is
  raw JSON-RPC (free 3–6M credits/day), which would mean building our own
  token indexer. Rejected: slowest to build, no price data.
- **Zerion API** — has a ready-made wallet PnL endpoint and DeFi positions,
  but the free tier is only 2,000 calls/month and paid starts at $149/mo.
  Kept as optional low-frequency PnL sanity check, not primary.
- **Moralis** — viable runner-up (free 40k CU/day, PnL endpoints), but
  requires attribution on the free tier, per-chain calls, and its PnL is
  swap-based/realized-only.
- **Do nothing (keep Bitfinex bot)** — does not serve the actual need.

## Implementation Notes

- Full comparison table, architecture sketch, and source links live in
  `docs/DESIGN.md`.
- Planned layout: `providers/alchemy.py`, `providers/etherscan.py`,
  `store.py` (SQLite), `pnl.py`, `report.py`; config in
  `~/.hrusha/config.yaml` (addresses + API keys; created manually by the
  owner, lives outside the repo, never committed).
- Transfers between the owner's own addresses must be flagged so they are
  not counted as spend/income in PnL.
- v1 scope: Ethereum mainnet, read-only, CLI. L2s are cheap to add later —
  both chosen providers cover them with the same API key.

## Follow-ups

- Implement the provider clients and SQLite store; none of the new code
  exists yet (this entry covers the purpose/design change only).
- ~~Remove or archive the legacy Bitfinex modules once the monitor
  works.~~ Done 2026-07-05 at the owner's request — deleted outright
  (recoverable from git history at `4912d87` and earlier); the
  `bitfinex-api-py` dependency removed with it.
- Pricing/limits researched July 2026 — recheck free-tier terms before any
  polling-frequency increase.
