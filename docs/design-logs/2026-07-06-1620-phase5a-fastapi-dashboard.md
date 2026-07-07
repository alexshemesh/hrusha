---
date: 2026-07-06T16:20
type: feature
status: merged
trigger: dependency
touches:
  - hrusha/service/app.py
  - hrusha/service/templates/
  - hrusha/ledger/reports.py
  - hrusha/cli.py
  - pyproject.toml
related:
  - 2026-07-06-1534-ledger-hardening-nft-doctor-rules-backup.md
supersedes: null
commit: 0f6b9ca
pr: https://github.com/alexshemesh/hrusha/pull/10
---

# Phase 5a: FastAPI dashboard — server-rendered pages over the ledger

## Context

Phases 1–4c produced a trustworthy ledger (now doctor/heal-verified) and
CLI reports. The plan's Phase 5 promises a web dashboard; the operator's
actual daily questions are "what am I holding, what's claimable, when
does the epoch flip, what did each source net" — all already answered by
existing ledger queries.

## Problem / Goal

A browsable view of the ledger without introducing a second source of
truth or client-side complexity that would need its own maintenance.

## Decision

- FastAPI + Jinja2, **server-rendered pages only** (no SPA, no JS
  beyond browser form handling): every page is a plain render of the
  same `ledger/reports.py` queries the CLI uses, so dashboard and CLI
  can never disagree.
- New pinned dependencies: fastapi, uvicorn, jinja2, python-multipart
  (form posts). This is the planned Phase 5 stack per
  docs/IMPLEMENTATION_PLAN.md.
- Pages: Overview (latest-sync snapshots: positions/claimables/balances,
  epoch countdown, staleness nudge), Income (neto per epoch × source,
  USD and coin views, drill-down links into filtered transfers),
  Transfers (epoch/source/tag filters, inline manual tagging, gas
  summary). Refresh button runs a full sync on a background thread —
  one at a time, outcome shown on next page load.
- `create_app(config, sync_runner=None)` factory: explicit wiring,
  injectable sync for tests; threads open their own SQLite connections.
- Security: `hrusha serve` binds 127.0.0.1 by default and warns loudly
  on any other host — the app has no auth or CSRF by design (personal,
  local tool). API docs endpoints are disabled. Spam-token symbols are
  attacker-controlled; Jinja autoescape covers every render (tested).
  The tag form's redirect is reduced to path+query so a forged Referer
  cannot bounce the browser off-origin.

## Alternatives Considered

- **Static HTML report generation** — simpler still, but the plan's
  refresh button and inline tag editing need a server anyway; a
  read-only artifact would grow a server the moment either landed.
- **SPA (React/htmx-heavy)** — rejected: one operator, localhost, and
  the CLI already defines the data shapes; client state would only add
  drift risk.
- **Flask** — equivalent fit; FastAPI chosen per the implementation
  plan (typed request params, TestClient, uvicorn story).

## Implementation Notes

- Overview reads `latest_snapshots()`: all snapshot rows within 600 s of
  the newest, because one sync writes its snapshot groups seconds apart.
- Dust/spam balances under $0.01 are hidden on the page (the CLI still
  shows everything) — display policy, not data policy.
- Templates ship as package data (`[tool.setuptools.package-data]`).
- Verified live against the real ledger: positions/claimables totals,
  epoch countdown, income-per-epoch drill-down into filtered transfers.
- Security pass on the same branch: cross-site form POSTs rejected by an
  Origin check (any site can make the operator's browser POST to
  localhost), referer-based redirects reduced to safe same-origin paths,
  hostile token `decimals()` bounded in heal.
- The strategy-profit view (overview headline + `/strategy/{source}`)
  forced three data fixes it immediately validated against known ground
  truth (40acres lifetime yield +$606, hand-derived earlier):
  - **DefiLlama chart timestamps jitter seconds before midnight**, so
    prices were cached under the previous day and the intended day was
    poisoned as a definitive miss — the true root cause of the
    "unpriced legs" backlog. Points now round to the nearest day.
  - **`hrusha reprice`**: purges cached misses and backfills
    `usd_at_time`/`gas_usd`; recovered 332 transfers + 24 fees live.
  - **Router-paired vault flows** (`_pair_router_vault_flows`): Morpho
    deposits go through a bundler, so the asset leg never touches the
    vault address; the sibling share mint/burn in the same tx now lends
    it its tag and source. Alchemy Portfolio balances also paginate now
    (spam airdrops pushed real tokens past page 1).
- Strategy grouping policy (display-level): rebases and veNFT purchases
  count under aerodrome-voting. USD-at-time makes in-kind winners with
  falling collateral (Morpho in ETH terms) look negative — explicitly
  deferred to the income/spend semantics discussion.

## Follow-ups

- Phase 5b: scheduler (hourly + epoch-flip cadence), alerts panel
  (large transfers, gas spikes), voted-this-epoch indicator (needs a
  live VeSugar read on the overview).
- Docker wiring for `serve` arrives with Phase 6 compose.
