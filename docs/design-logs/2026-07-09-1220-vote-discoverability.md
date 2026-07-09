---
date: 2026-07-09T12:20
type: bugfix
status: proposed
trigger: workaround
touches:
  - hrusha/adapters/known_contracts.py
  - hrusha/service/vote_scout.py
  - hrusha/service/templates/votes.html
  - tests/test_vote_scout.py
  - tests/test_app.py
related:
  - 2026-07-07-1126-vote-scout-probe.md
supersedes: null
superseded_by: null
commit: null
pr: null
---

# Align vote suggestions with Aerodrome pool migration state

## Context

The vote scout intentionally scans the complete FactoryRegistry index space rather than copying Aerodrome's frontend list. That decision finds every alive gauge, but Aerodrome now distinguishes legacy Slipstream pools that are eligible for or undergoing migration to newer gauges. Its default vote scope hides those pools.

Aerodrome also carries vote allocations across epoch boundaries, while Hrusha used only the current epoch's `voted_at` timestamp and displayed `NOT voted` immediately after rollover.

## Problem / Goal

The scout recommended pools that were difficult to locate in Aerodrome's default vote screen, including pools Aerodrome labeled **Migrating**. At the same time, it described carried-forward allocations as absent. Both behaviors reduce confidence in financial guidance even though the underlying reward and vote data are real.

## Decision

Attach the source factory to each Sugar pool row by paging within factory boundaries. Treat explicitly known legacy Slipstream factories as migrating and add a blocking `MIGRATING` risk flag.

Separately expose whether VeSugar reports active pool allocations. The dashboard will distinguish a vote recast in the current epoch, a carried-forward allocation, and no allocation.

## Alternatives Considered

- **Consume Aerodrome's frontend pool API** — rejected because it is undocumented and would make the scanner depend on a private application contract.
- **Call `factory()` on every deep-scanned pool** — rejected because the sequential scan is already RPC-latency bound and the factory is implicit in Sugar's concatenated index space.
- **Keep recommendations and only document “All pools”** — rejected because migration status is a material eligibility signal, not merely navigation help.
- **Do nothing** — rejected because the current UI can recommend a pool while simultaneously making it appear nonexistent or inactive to the operator.

## Implementation Notes

- Split each factory's global index range into at-most-200-index `epochsLatest` calls. Do not infer end-of-data from returned row count because Sugar drops dead gauges.
- Store public legacy factory addresses in `known_contracts.py`; never derive migration state from token symbols or pool names.
- Keep migrating rows in the full candidate table, visibly flagged, so the exclusion is explainable.
- VeSugar's existing `votes` output is the allocation source; `voted_at` remains only the “recast this epoch” signal.
- The Aerodrome link is informational and read-only. Hrusha will not connect wallets or submit votes.

## Follow-ups

- Add scan phase/progress reporting separately; the registry now exceeds 34,000 pool indexes and the old three-minute estimate is stale.
- Revalidate the explicit legacy-factory set when Aerodrome announces another gauge migration.
