---
date: 2026-07-08T12:00
type: feature
status: implemented
trigger: architecture | api-change | new-pattern
touches:
  - hrusha/service/invest_scout.py
  - hrusha/providers/aave.py
  - hrusha/cli.py
  - tests/test_invest_scout.py
related:
  - 2026-07-07-1126-vote-scout-probe.md
  - 2026-07-06-1117-blockscout-defillama-providers.md
supersedes: null
commit: pending
pr: pending
---

# Read-only investment suggestions with transparent risk tags

## Context
hrusha is a crypto income monitor (tracks earned income on Base). User
wants a read-only report suggesting where to deploy idle ETH, cbBTC, USDC
into low-risk, fast-withdrawable positions. Existing providers: DefiLlama
(historical prices, free, no key), Alchemy (balances, eth_call, already
integrated), Morpho GraphQL adapter (vault positions).

vote_scout.py is the established pattern: chain I/O separated from scoring,
risk flags attached as tags, pools with any flag never reach the suggested
list. invest_scout follows the same shape but does NOT auto-filter — user
explicitly wants all options shown with risk tags, nothing hidden.

## Problem / Goal
User wants safe deployment options for idle balances, with full
transparency: every quirk and risk tagged, no silent filtering. Read-only
— no execution, no signing, no key handling beyond read-only Alchemy calls.

## Decision
New service module invest_scout.py + Aave/Seamless on-chain provider.
Reads real balances from the ledger, queries lending/staking/LP
opportunities, attaches risk tags, prints a ranked table. Staking shown
but tagged withdrawal-slow — not hidden. Safe? column is informational,
not a filter.

Risk tags: sc-risk, base-native, curated/uncurated, depeg-risk,
il-low/il-high, beacon-queue, withdrawal-slow, utilization-high,
liquidity-low, audited, quirk:*.

APYs from DefiLlama yields API. Liquidity/safety from Alchemy eth_call.
Morpho via existing GraphQL adapter.

## Alternatives Considered
- **Execute deposits too** — rejected, huge risk surface, user wants
  read-only first.
- **Hide unsafe options** — explicitly rejected by user; all options shown
  with tags.
- **DefiLlama-only (no on-chain liquidity check)** — rejected; APY without
  withdrawal-safety check hides the main risk user cares about.
- **Include only lending, exclude staking** — rejected; user wants to see
  everything and decide, with staking tagged withdrawal-slow.

## Implementation Notes
Follows vote_scout structure: pure scoring/tagging functions take already-
fetched data, unit-testable without network. Provider modules do chain I/O.
CLI subparser `invest` added alongside existing subparsers.

## Follow-ups
- Execute-deposit flow (future, separate entry)
- Stable LP quirk detection (concentrated ranges, out-of-range risk)
- Caching APY snapshots for the dashboard
