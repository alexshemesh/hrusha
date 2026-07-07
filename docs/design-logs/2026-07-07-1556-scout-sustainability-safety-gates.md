---
date: 2026-07-07T15:56
type: feature
status: merged
trigger: dependency
touches:
  - hrusha/service/vote_scout.py
  - hrusha/config.py
  - docs/examples/goplus_probe.py
related:
  - 2026-07-07-1126-vote-scout-probe.md
supersedes: null
commit: 140e11d
pr: https://github.com/alexshemesh/hrusha/pull/12
---

# Vote scout: sustainability + bribe-quality + token-safety gates

## Context

The filter lab justified turning the majors-only pair gate off (it cost
~32% of simulated earnings for zero stability gain), which opened the
suggested list to exotic pools. Reputation-based screening is gone, so
the exotic universe needs MECHANICAL screening instead. Internet survey
of ve(3,3) analytics and rug-screening tooling produced four candidates
the operator picked for implementation.

## Decision

Four new signals on /votes. ONE-OFF-BRIBE and TOKEN-RISK are blocking
flags; EMISSIONS-SUBSIDIZED and SELF-BRIBED are INFORMATIONAL notes
(muted pills, never block) — the operator's call after challenging the
self-bribe block: legit standing self-token programs exist (live
specimen: vAMM-WETH/VVV, 6/6 bribed epochs with real fees), and
neither pattern's realized penalty has been back-tested. Notes say
"know what you're being paid in", flags say "do not touch":

- **EMISSIONS-SUBSIDIZED** (config `min_fees_per_emission`, default
  0.1): accrued fees vs the AERO emitted to the gauge over the SAME
  window (rate x elapsed epoch seconds x AERO spot). A pool far in
  deficit only has a vAPR while emissions keep flowing — rented, not
  earned. `emissions` was already in every LpEpoch decode, previously
  discarded.
- **ONE-OFF-BRIBE** (constants): incentives drive >30% of the reward
  but fewer than 2 of the recent completed epochs had any bribes —
  a pump, not a program. Uses the epochsByAddress history already
  fetched for vote projection.
- **SELF-BRIBED** (constant, >50% share): incentive USD paid in the
  pool's own non-major pair token — a project paying voters with the
  token they'd have to sell into the project's own thin liquidity.
- **TOKEN-RISK** (config `token_safety`, default on): GoPlus token
  security (free, keyless, Base chain id 8453) on all non-major pair
  and bribe tokens of the candidates. Only MECHANICAL findings gate:
  honeypot, cannot_sell_all, transfer_pausable, is_blacklisted,
  owner_change_balance, selfdestruct, trading_cooldown, hidden_owner,
  buy/sell tax >5%.

## Alternatives Considered

- **DefiLlama yields API** (sigma/outlier/il7d/ML predictions) —
  deferred: pool-id-to-address mapping is fuzzy by symbol; revisit if
  the four gates prove insufficient.
- **Holder-concentration / mintable / proxy flags** — rejected as
  gates: AERO itself reads mintable=1 with top10=67%, USDC is a proxy;
  these would nuke every legit major. Kept informational in the probe.
- **Voter-concentration & vote-timing analysis** — rejected: needs
  per-pool Voted event scans (expensive, per the vote history probe);
  the dilution projection already covers most of that risk.

## Implementation Notes

- GoPlus gotcha (docs/examples/goplus_probe.py, live 2026-07-07): the
  unauthenticated BATCH endpoint silently drops results (5 asked, 1
  answered) — integration queries ONE token per call (~0.4s, ~20-30
  non-major tokens per scan). Positive findings only: a token GoPlus
  never scanned is unknown, not risky (age/TVL/pricing gates carry
  that). If every call fails, `ScoutResult.token_safety_checked` goes
  False and the page shows a loud banner — silently unchecked must
  never read as "all clean".
- Fees/emissions uses the same accrual window on both sides (epoch
  start to now), so the ratio is fair at any point mid-epoch.
- One-off-bribe and self-bribe thresholds are constants, not config:
  they are boolean judgments, not curves worth lab-tuning.

## Follow-ups

- Re-run docs/examples/pool_filter_lab.py including fees/emissions to
  tune `min_fees_per_emission` against realized outcomes (the 0.1
  default was sanity-checked against one live scan, not back-tested).
- GoPlus responses could be cached across scans (token risk barely
  changes week to week) once the Phase 5b/5c scheduler exists.
