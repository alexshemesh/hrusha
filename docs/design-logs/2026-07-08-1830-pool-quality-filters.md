# 2026-07-08 1830 — Pool Quality Filters

## Problem

The invest scanner recommended a dead Aerodrome Slipstream MSUSD-USDC pool (CL1, 0.01% fee tier)
as the #1 "best safe" USDC option. The pool had:
- **0 holders** (per Aerodrome UI)
- **$42,428 TVL** — too small to deploy meaningful capital
- **49.41% APY** entirely from incentive rewards (`apyBase=None`, `apyReward=49.41%`)
- **Zero 7-day trading volume** (`volumeUsd7d=None`) — no fee revenue
- **`outlier=True`** — DefiLlama's own anomaly flag
- **Predicted to decline** — DefiLlama predictions: Down, confidence 3/3

The scanner sorted by spot APY with no quality filtering, so transient reward-farmed spikes
on dead pools dominated the recommendations.

## Decision

### Hard filters (drop from results entirely)
1. **`outlier == True`** — trust DefiLlama's anomaly flag
2. **`tvlUsd < $50,000`** — too small to deploy capital without slippage
3. **`volumeUsd7d is None or 0`** (stable-lp only) — dead LP pool, zero fee revenue

### Risk tags (keep visible, flag with tag)
4. **`reward-only`** — `apyBase` is 0/None but `apyReward > 0` (100% incentive farming)
5. **`apy-declining`** — DefiLlama `predictedClass == "Down"` and `binnedConfidence >= 2`
6. **`apy-volatile`** — spot APY > 2× the 30-day average (transient spike)

### Ranking change
7. **Sort by `apyMean30d`** instead of spot APY (falls back to spot when 30d is unavailable)

## Data added to RawOpportunity
- `apy_30d_pct` — 30-day average APY for ranking
- `volume_7d_usd` — 7-day trading volume for dead-pool filter
- `apy_base_pct` / `apy_reward_pct` — organic vs reward yield split
- `is_outlier` — DefiLlama anomaly flag
- `prediction_class` / `prediction_confidence` — yield direction prediction

## Files changed
- `hrusha/service/invest_scout.py` — new RawOpportunity fields, yield-quality tags in tag_risks(),
  rank() sorts by 30d APY
- `hrusha/providers/aave.py` — hard filters in fetch_opportunities(), pass-through of DefiLlama metadata
- `hrusha/service/templates/invest.html` — 30d avg column in both tables
- `tests/test_invest_scout.py` — 4 new tests (reward-only, apy-declining, apy-volatile, 30d ranking)

## Trade-offs
- Hard filtering contradicts the "show everything with tags" principle, but dead pools with 0
  volume and 0 holders are not real opportunities — they're data artifacts. The tags handle
  pools that are real but risky.
- `apyMean30d` is a lagging indicator. A pool that recently improved its yield will be
  underranked. Acceptable trade-off vs. ranking transient spikes first.

## Status
implemented
