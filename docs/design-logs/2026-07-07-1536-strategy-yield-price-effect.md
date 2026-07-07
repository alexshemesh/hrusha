---
date: 2026-07-07T15:36
type: feature
status: proposed
trigger: tradeoff
touches:
  - hrusha/ledger/reports.py
  - hrusha/service/templates/overview.html
related:
  - 2026-07-06-1620-phase5a-fastapi-dashboard.md
supersedes: null
commit: null
pr: null
---

# Strategy view: decompose profit into in-kind yield and price effect

## Context

The Phase 5a strategy table showed lifetime profit as pure
USD-at-event-time. The operator found Morpho's row ("−$323 profit")
misleading: "if I withdraw rewards from Morpho it's profit". This is
the income/spend-semantics discussion deferred since Phase 3/4 —
finally forced by a concrete specimen.

## Problem / Goal

Auto-compounding vaults (Morpho, 40acres, ExtraFi) never emit income
events — shares silently buy more of the asset, so `income` is
structurally $0 and the yield hides inside `taken out + holding`.
USD-at-event-time then mixes two unrelated stories in one number:
did the strategy perform, and did the asset's price move. Morpho had
EARNED +0.0055 WETH in-kind while showing −$323, all of it ETH's
price falling since deposit (and mostly unrealized).

## Decision

Keep the USD columns, add a two-part decomposition (operator's pick
from three options):

- **yield** — the strategy's earnings in its own coins:
  `withdrawals + holding − deposits` per asset family, valued at the
  latest cached daily price. Only true `deposit`/`withdraw` asset
  legs count; share mint/burn mirrors are already swap-skipped;
  native ETH legs fold into the WETH family (1:1). Positions complete
  the cycle; claimables stay out (they become income on arrival).
- **price effect** — `profit + gas − income − yield`: what holding
  the asset through the period did to the USD number. The identity
  `profit = income + yield + price effect − gas` holds by
  construction.
- Income-style strategies (aerodrome-voting: locks, veNFT purchases,
  same-day-sold claims) show "—": their capital enters in different
  tokens than it exits, so a single-asset in-kind cycle is undefined
  — and their income is already correct USD (operator sells claims
  the same day they arrive).
- If any asset in a family lacks a cached price, yield_usd and
  price effect are None rather than a partial sum that would silently
  misattribute the remainder.

Verified live: 40acres +607.16 USDC ($606.98) / price −$0.48
(stablecoin: the two views agree); ExtraFi +3.87 AERO ($2.19) /
price −$244.99; Morpho +0.0055 WETH ($9.78) / price −$332.04.

## Alternatives Considered

- **In-kind only (drop USD profit)** — rejected: hides real USD moves
  entirely; the operator still thinks in dollars.
- **Relabel columns, no new math** — rejected: Morpho would keep
  reading as a loss when the strategy itself earned.
- **Value yield at withdrawal-time prices instead of spot** —
  rejected for now: mixes realization timing back into the number the
  decomposition exists to isolate; spot answers "what is the earned
  coin worth today", matching the holding-now column.

## Implementation Notes

- `StrategyRow` gains `yield_items` (token, net coins, spot value),
  `yield_usd`, `price_effect_usd`; computed inside `strategy_summary`
  from the same event walk (no second pass over events).
- Spot prices come from `price_cache` (latest non-NULL day per token
  contract, `ETH` fallback for the WETH family) — reports stay
  pure-SQLite, no network.
- Coin flows use `amount_native` so unpriced-at-event legs still
  count exactly; `reinvest` vault-to-vault rotations net out by
  construction.
- Dust filter: families that round-trip to < 1e-6 coins or < $0.01
  are hidden from display but still inside `yield_usd`.
- ruff S105 false-positives (token symbol assignments read as
  "hardcoded password") are noqa'd with justification.

## Follow-ups

- An in-kind view for aerodrome-voting (AERO-denominated: locks +
  rebases + claims-in-AERO vs veNFT AERO held) if the operator wants
  the same story there; needs a cross-token convention for USDC-paid
  veNFT purchases.
- Surface the decomposition on the /strategy/{source} detail page.
