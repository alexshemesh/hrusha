# hrusha — Crypto Income Monitor: Design

*v2 — updated 2026-07-05 after design session. Supersedes the v1 generic
"ETH portfolio monitor" scope; provider research from v1 is kept at the
bottom.*

## Purpose

A service running on the owner's home computer that answers, at a glance:

> **How much am I making, net, and from which source?**

Income sources it must model:

1. **ve(3,3) voting** — locked AERO (veNFT) votes on Aerodrome (Base) every
   weekly epoch; bribes + fees are claimed the day after epoch flip, swapped
   to USDC / BTC / ETH representations, and reinvested. Velodrome (Optimism)
   and "a couple more networks" follow the same mechanics — later phases.
2. **Passive yield** — deposits in Morpho vaults (morpho.org) and on
   **40acres.finance** (a separate Base protocol: self-repaying loans
   against yield-bearing assets such as Aerodrome veNFTs; the owner puts
   tokens there to earn passive profit). Two distinct protocols, two
   adapters.
3. **CEX rewards** — Binance reward tokens and Bitfinex positions.
   *Explicitly out of scope for v1 code, but the data model must be ready
   for them (tagging, sources).*

Cross-cutting requirements:

- **Neto per source**: every income event valued in **USD at event time
  AND native coin amount** — coin amounts are first-class, not a footnote
  (a worthless coin today may matter next year).
- **Fees**: every gas fee tracked and attributed, so "where do I pay the
  least fees" is answerable.
- **Money movement**: transfers in/out of every tracked address, with
  own-address transfers recognized.
- **Tagging**: a tag system on every ledger event (source, counterparty,
  purpose) driving all reports — designed to absorb CEX data later.
- **Future**: automated reward claiming (requires signing — see Security).

## Decisions Made (2026-07-05 session)

| Topic | Decision |
|---|---|
| Networks v1 | **Base only** (Aerodrome + Morpho vaults live there). Optimism/Velodrome and others later. |
| Data provider | **Alchemy RPC + protocol adapters first**, isolated behind a provider interface so **DeBank paid API (~$100 tier)** can replace/augment it later — owner's experience: DeBank pays off and also brings prices with positions. |
| CEX | No Binance/Bitfinex code in v1. Data model + tags must support them. |
| Profit basis | **Realized USD at claim time, per epoch, per source** — with native coin amounts always shown alongside. Unrealized price moves reported separately. |
| UI | **Local web dashboard + Google Sheets export** (ledger tab + per-epoch summary tab, append-only, pivot-friendly). |
| Freshness | Hourly background sync + manual Refresh + faster polling around epoch flip. |
| Packaging | Developed on the owner's Mac, deployed to a different home machine as a **Docker image published to GitHub Container Registry (ghcr.io)** via GitHub Actions; `compose.yaml` + install script on the target. |
| Repo | **Public GitHub repo — security is paramount.** No addresses, keys, DB files, or sheet ids ever in repo, image, or CI logs; secret-scanning gates enforced (see Security & Privacy). |
| Database ops | SQLite lives in a mounted `/data` volume, never in the image; forward-only auto-migrations on startup; nightly backups with documented restore (see DB Management). |
| Tests | First-class: unit + adapter tests on recorded fixtures run in CI with zero secrets; live smoke tests are local-only commands. |
| Alerts v1 | Large/unexpected transfers; gas-spike warning ("claiming now is expensive"). No epoch reminder — owner uses Google Calendar for that. Shown in UI; Telegram/email later. |
| Addresses | 2–4 tracked addresses; all operations (claims, swaps, deposits) happen from these same addresses, so auto-tagging can rely on tx locality. |

## Architecture

Long-running Python service (systemd/launchd on the home machine):

```
hrusha/
  service/
    scheduler.py     # hourly sync; denser around epoch flip (Thu 00:00 UTC)
    app.py           # FastAPI: dashboard + JSON API + manual refresh
  providers/
    interface.py     # DataProvider: balances, transfers, positions,
                     # claimables, prices — the DeBank swap point
    alchemy_rpc.py   # v1 impl: Portfolio API (balances+prices),
                     # getAssetTransfers, eth_call for adapters
    debank.py        # later: positions + claimables + prices in one place
  adapters/          # protocol-specific contract reads (used by alchemy_rpc)
    aerodrome.py     # veNFT locks & votes, gauges, claimable bribes/fees
                     # (Aerodrome "Sugar" helper contracts make this cheap)
    morpho.py        # vault shares -> underlying + accrued yield
                     # (Morpho public GraphQL API is a free shortcut here)
    forty_acres.py   # 40acres.finance positions on Base (supply/earn side;
                     # small protocol, expect contract reads over docs)
  ledger/
    store.py         # SQLite: events, snapshots, tags, epochs
    tags.py          # rule-based auto-tagging + manual overrides via UI
    reports.py       # neto per source per epoch; fees; flows; USD + coins
  export/
    sheets.py        # Google Sheets: append-only ledger + epoch summary
  web/               # dashboard templates/assets
  main.py            # CLI: run service, one-shot sync, report, export
```

### Ledger data model (the core)

Append-only `events` table; every row:

- `ts`, `chain`, `tx_hash`, `block`
- `kind`: reward_claim | swap | deposit | withdraw | transfer_in |
  transfer_out | gas_fee | vote
- `token`, `amount_native`, `usd_at_time` (both always stored)
- `gas_native`, `gas_usd` (for the tx that caused the event)
- `source`: aerodrome-voting | morpho | manual/cex-... (extensible)
- `tags`: free-form list (e.g. `own-transfer`, `to-binance`, `reinvest`,
  `low-risk`) — auto-rules first, manual edits in UI win
- `epoch_id` (Aerodrome weekly epoch the event belongs to)

Reports = queries over this table: **neto per source per epoch**, fees per
chain/source, in/out flows, all shown as USD + native amounts.

### Epoch awareness

Aerodrome epochs flip Thursday 00:00 UTC; voting happens the night before,
claiming after. The service knows the epoch calendar: dashboard shows a
countdown, reporting groups by epoch, and sync frequency increases in the
hours around the flip.

### Prices

USD-at-event-time comes from Alchemy's Prices API (free tier) with
CoinGecko as fallback for long-tail tokens; when DeBank arrives it becomes
the primary price source for positions.

### Google Sheets export

Two tabs, append-only, written by the service on schedule:

1. **Ledger** — one row per event (all fields above, incl. tags).
2. **Epoch summary** — one row per (epoch × source): gross USD, gas USD,
   neto USD, plus native coin totals.

Auth via a Google service account; the sheet is shared to it. Nothing is
ever overwritten, so manual analysis on top is safe.

### Packaging & Deployment

Development happens on the owner's Mac; production is a different home
machine. The bridge is a Docker image:

- **Image**: multi-stage build on `python:3.12-slim`, non-root user,
  `HEALTHCHECK` hitting the service's health endpoint. The image contains
  **code only** — no config, no keys, no data.
- **Registry**: GitHub Container Registry (`ghcr.io/<owner>/hrusha`),
  pushed by GitHub Actions on version tags (`v*`) using the built-in
  `GITHUB_TOKEN` (`packages: write`); `latest` + semver tags.
- **Target machine**: `compose.yaml` runs the service with two mounts —
  `/data` (SQLite, backups) and `/config` (read-only: `config.yaml`,
  Sheets service-account JSON). A small `deploy/install.sh` bootstraps a
  fresh machine: create dirs, pull image, install a systemd unit (or
  launchd plist) that wraps `docker compose up -d`.
- **Upgrade path**: pull new tag, `docker compose up -d` — migrations run
  automatically on startup (see DB Management). Rollback = previous tag
  (migrations are forward-only, so a DB backup is taken before migrating).
- **Dashboard exposure**: binds to `127.0.0.1` by default; set to the LAN
  interface explicitly if desired. It displays your finances — it must
  never be reachable from the internet; no auth is built in v1, so network
  isolation IS the auth. Documented prominently in the README.

### DB Management (guidelines)

- **Location**: single SQLite file at `/data/hrusha.db` (host bind mount).
  The DB is *derived state* — everything except manual tags can be
  re-synced from chain; manual tags make backups worthwhile.
- **Migrations**: `schema_version` table; forward-only, applied
  automatically at service startup; each migration is a numbered SQL file
  in `ledger/migrations/`. A pre-migration backup is taken automatically.
- **Backups**: nightly `sqlite3 .backup` to `/data/backups/`, 30-day
  rotation. Manual: `hrusha db backup` / `hrusha db restore <file>`.
- **Health**: `hrusha db check` runs `PRAGMA integrity_check` +
  `quick_check`; surfaced in the dashboard.
- **Moving machines**: stop service → copy `/data` → start on new machine.
  Nothing else carries state.

### Security & Privacy (public repo — paramount)

Threat model: the repo and image are public; the data (addresses, keys,
balances, income) is private. The two must never mix.

1. **Never in git**: `~/.hrusha/` is outside the repo by construction;
   `.gitignore` additionally blocks `config.yaml`, `*.db*`, `.env*`,
   `*service-account*.json`, `/data`, `artifacts/`. `.dockerignore`
   mirrors this so nothing leaks into image layers.
2. **Secret scanning, twice**: `gitleaks` as a pre-commit hook (local) and
   as a CI job (gate on PRs). GitHub repo settings: enable secret scanning
   + push protection.
3. **CI has no secrets**: tests run on recorded fixtures; no API keys
   exist in Actions at all (the image build needs none). Live smoke tests
   (`hrusha smoke`) are local-only and excluded from CI.
4. **Image hygiene**: config/data volumes mounted at runtime only; no
   build args carrying secrets; image scanned by Dependabot/`docker scout`.
5. **Docs hygiene**: no addresses, tx hashes, balances, sheet ids, or
   amounts in committed docs, design logs, tests, or fixtures — fixtures
   use synthetic addresses. (Recorded RPC fixtures must be scrubbed:
   replace real addresses before committing.)
6. **Logs**: may contain addresses; they stay on the machine (stdout →
   local journald/docker logs), never shipped anywhere.
7. **Read-only forever in v1**: no private keys exist in this system.
   Future auto-claiming is a separate module with its own design review:
   dedicated hot wallet with minimal gas ETH, contract allowlist, dry-run
   mode — and its key will live only on the target machine, never in git,
   image, or CI.
8. **Residual, accepted**: the repo publicly documents the *strategy*
   (which protocols, voting cadence) but not identities, addresses, or
   amounts. Anyone can read how it works; nobody can tell it's you or how
   much is involved.

## Open Questions

1. Which "couple more networks" for later ve(3,3) phases (affects nothing
   in v1).

### Resolved (2026-07-05)

- 40acres position type: **supply side confirmed** — the owner lends
  crypto that funds other users' self-repaying loans and earns interest.
  `forty_acres.py` models supplied principal + accrued interest; income
  events are interest accruals/claims, source tag `40acres`.
- Third alert: none — epoch reminders live in the owner's Google Calendar.
  v1 alerts are exactly: large/unexpected transfers, gas-spike warning.
- 40acres = 40acres.finance, a standalone Base protocol (not a Morpho
  vault) — gets its own adapter.
- 2–4 tracked addresses; all operations happen from these addresses
  (auto-tagging can rely on tx locality — swaps after claims are safely
  taggable as `reinvest`).

## Provider Research (July 2026, from v1 — still valid)

| Provider | Cost | Balances + prices | Transfers | Gas fees | DeFi positions / PnL | Verdict |
|---|---|---|---|---|---|---|
| **Alchemy** | Free: 30M CU/month | ✅ one call/address, USD prices, spam-filtered | ✅ `getAssetTransfers` | ⚠️ via receipts | ❌ none — hence our adapters | **v1 primary** |
| **Etherscan V2** | Free: 5 req/s, 100k/day, Base incl. | ⚠️ no USD prices | ✅ | ✅ `gasUsed × gasPrice` | ❌ | Fee accounting + cross-check |
| **DeBank Cloud** | Paid units, ~$100 advanced tier | ✅ richest, incl. prices | ✅ | ✅ | ✅ positions + claimables | **Planned upgrade** behind provider interface |
| **Zerion** | Free: 2k calls/month | ✅ | ✅ | ⚠️ | ✅ incl. PnL endpoint | Not selected (call budget too small for hourly sync) |
| **Moralis** | Free: 40k CU/day, attribution | ✅ | ✅ | ⚠️ | ⚠️ swap-based PnL | Runner-up, not selected |
| **MetaMask/Infura** | Free RPC credits | ❌ raw RPC only | ❌ | ✅ | ❌ | Rejected — no portfolio API |

## Sources

- Alchemy pricing / Portfolio API: <https://www.alchemy.com/pricing>, <https://www.alchemy.com/docs/reference/portfolio-apis>
- Etherscan V2 limits: <https://docs.etherscan.io/resources/rate-limits>
- DeBank Cloud OpenAPI: <https://docs.cloud.debank.com/en/readme/open-api>
- Zerion API / PnL: <https://zerion.io/api>, <https://developers.zerion.io/api-reference/wallets/get-wallet-pnl>
- Moralis pricing / PnL: <https://moralis.com/pricing/>, <https://docs.moralis.com/data-api/data-features/data-enrichment/profitability-pnl>
- Infura/MetaMask limits: <https://docs.metamask.io/services/get-started/pricing/>
- Aerodrome docs: <https://aerodrome.finance/docs>; Morpho: <https://docs.morpho.org/>
