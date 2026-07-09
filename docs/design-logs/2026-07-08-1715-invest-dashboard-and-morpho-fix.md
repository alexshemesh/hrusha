---
date: 2026-07-08T17:15
type: feature
status: merged
trigger: api-change | new-pattern | bugfix
touches:
  - hrusha/service/app.py
  - hrusha/service/templates/invest.html
  - hrusha/service/templates/base.html
  - hrusha/service/invest_scout.py
  - hrusha/providers/aave.py
  - tests/test_app.py
related:
  - 2026-07-08-1200-invest-scout-suggestions.md
  - 2026-07-06-1620-phase5a-fastapi-dashboard.md
supersedes: null
commit: pending
pr: pending
---

# Invest suggestions on the web dashboard + Morpho vault symbol fix

## Context
The invest-scout service (`2026-07-08-1200-invest-scout-suggestions.md`) shipped
as a CLI command only (`hrusha invest-suggestions`). The FastAPI dashboard
already had a votes-scan page with a background-thread pattern
(`ScoutState` + `/votes/scan`), but no invest surface. Separately, a live test
showed Morpho ETH lending vaults (~2% APY) were absent from scan results despite
being real and sizable on Base.

## Problem / Goal
1. Surface invest suggestions on the dashboard so the user can see idle-capital
   deployment options in the same UI as the rest of the monitor, with a scan
   button (live DefiLlama data, ~5s).
2. Fix the silent drop of Morpho / Gauntlet / Steakhouse lending vaults.

## Decision
**Dashboard.** Mirror the existing votes-scan pattern exactly: a new
`InvestState` dataclass (lock / running / result / last_error), a
`default_invest_runner` that wires `AlchemyProvider` → `aggregate_balances` →
`scan`, a `GET /invest` page and `POST /invest/scan` that spawns a daemon
background thread and redirects. `create_app` gained an `invest_runner` param
for test injection. A new `invest.html` template renders idle balances, a
"best safe option per token" summary, and a full ranked opportunities table
with risk-tag pills — nothing hidden, consistent with the CLI's
no-auto-filter rule. The nav gained an `invest` link.

**Morpho fix.** `_map_token` in `providers/aave.py` matched DefiLlama symbols
by exact equality (`WETH`, `USDC`). Morpho vaults issue composite share-token
symbols (`MWETH`, `GTWETHB`, `STEAKUSDC`, `MWCBBTC`), so every vault was
silently dropped. Changed to substring matching with explicit cbETH exclusion
(user wants ETH / cbBTC / USDC only). This also newly maps `wstETH` / `stETH`
→ ETH and `WBTC` / `CBBTC` → cbBTC — the same root-cause fix applied
consistently rather than special-casing ETH alone.

**Dedup refactor.** The "best safe per token" dedup existed in
`render_table()` (CLI) and was duplicated in the template as a Jinja list-append
hack. Extracted `InvestResult.best_safe_by_token` (a property returning the
first safe option per token) and used it in both `render_table` and the
template, removing data-processing logic from the view layer.

## Alternatives Considered
- **Do nothing on the dashboard** — invest stays CLI-only. Rejected: the
  dashboard is the primary interface and idle-capital suggestions are
  high-value; a CLI-only feature is effectively hidden.
- **Synchronous scan on `GET /invest`** — rejected: even ~5s of DefiLlama HTTP
  would block the page; the votes pattern already proved the background-thread
  approach and reusing it keeps the codebase consistent.
- **Hardcode a Morpho vault allowlist** instead of substring matching —
  rejected: fragile, requires manual maintenance as new vaults launch;
  substring matching is general and covers Gauntlet/Steakhouse too.
- **Fix only ETH mapping** (narrowest fix for the reported "morpho ETH 3%")
  — rejected: would leave USDC and cbBTC Morpho vaults silently broken,
  reproducing the same bug class for the other two tokens.
- **Leave the Jinja dedup hack** — rejected: duplicated algorithm, data
  processing in the template, and cleverness, all of which the coding-guidance
  skill flags. The property is a single source of truth for both renderers.

## Implementation Notes
- `default_invest_runner` mirrors `default_scout_runner`: deferred import of
  the provider so read-only pages never pull heavy chain I/O.
- `/invest/scan` calls `_reject_cross_site(request)` like `/votes/scan` — no
  untrusted input accepted; Jinja2Templates auto-escapes, so external
  DefiLlama risk-tag strings are escaped.
- `_run_invest_thread` logs with `exc_info` and stores only the exception
  class name in `last_error` (no exception text leaked to the page).
- `best_safe_by_token` is a `dict[str, OpportunityScore]` preserving
  opportunities-list order (highest APY first); both renderers rely on that
  ordering for "best" to mean "highest-APY safe."
- cbETH exclusion lives in `_map_token` (`if "CBETH" in s: return None`), not
  in the scanner, so it applies to every future token source.

## Follow-ups
- Derive a real APY for the 40acres AERO-USDC vault (share-price appreciation
  from `epochRewardsLocked` drips / ledger snapshot history); DefiLlama does
  not index it, so it is currently invisible to the scout.
- Consider a per-row confidence grade (high/medium/low/unknown) driven by
  protocol maturity, TVL size, and utilization-known flags — currently every
  row is presented as equally trustworthy, which is not accurate.
