---
date: 2026-07-07T11:26
type: feature
status: merged
trigger: new-pattern
touches:
  - docs/examples/vote_scout_probe.py
  - hrusha/adapters/known_contracts.py
  - hrusha/service/vote_scout.py
  - hrusha/service/app.py
  - hrusha/service/templates/votes.html
related:
  - 2026-07-06-1620-phase5a-fastapi-dashboard.md
supersedes: null
commit: d1c8c91
pr: https://github.com/alexshemesh/hrusha/pull/11
---

# Vote scout: dilution-aware Aerodrome voting-pool suggestions (spike)

## Context

The operator votes veAERO manually each epoch, picking the highest
displayed vAPR and "hoping for the best". Displayed vAPR is a live
ratio (rewards so far / votes so far) that keeps moving until the
epoch closes — a tiny pool showing a huge vAPR collapses when late
voters pile in, and the displayed number never warns about that.

## Problem / Goal

Suggest voting pools before each epoch's cutoff with the risk story
attached: expected $ per vote AFTER projected late-vote dilution, pool
TVL, reward composition (real fees vs revocable incentives), reward
token quality, and pair quality. Read-only spike first, per the
project's probe-before-adapter convention.

## Decision

`docs/examples/vote_scout_probe.py` — a read-only probe that ranks
every alive gauge on Base and prints a flagged table plus a clean
shortlist with the operator's own vote power in the denominator.

Data sources (no new dependencies):
- `RewardsSugar.epochsLatest` — running epoch's votes + bribes + fees
  per pool; `epochsByAddress` — the pool's completed epochs (final
  votes are known there, which is what makes dilution projectable).
- Pool contracts directly (`token0/token1/balanceOf`) for TVL —
  deliberately NOT LpSugar: the deployed LpSugar is unverified and its
  struct layout varies by release, while two-token balances are
  version-proof.
- DefiLlama `prices/current` batch endpoint for spot USD (the ledger's
  price provider already trusts DefiLlama; the `confidence` field
  doubles as a reward-token-quality signal).

Scoring model:
- projected final votes = max(votes so far, median of last 6 completed
  epochs' final votes) — the anti-last-minute-dilution core.
- $/1k votes = current rewards / projected votes; a stress column
  recomputes at 1.15× the historical max votes.
- Risk flags, each a hard gate for the "SUGGESTED" list: TVL <
  $300k, <3 completed epochs of history, vote volatility (CV > 0.6),
  unpriced/low-confidence reward tokens, fees <10% of rewards (pure
  incentive farming), non-major pair tokens (symbol-level preference,
  not a security boundary — the TVL and pricing gates do that work).

## Alternatives Considered

- **LpSugar for TVL/pool metadata** — rejected: Base deployment is not
  source-verified and the `Lp` struct gained fields across releases;
  a silent field misalignment would corrupt every downstream number.
- **DefiLlama yields API for TVL** — rejected: pool identifiers don't
  map cleanly to Base pool addresses; on-chain reserves are exact.
- **Trusting displayed vAPR (do nothing)** — rejected: it is exactly
  the trap this exists to avoid; it ignores dilution and token quality.

## Implementation Notes

- `epochsLatest(_limit, _offset)` paginates a POOL-INDEX window over
  the concatenation of every FactoryRegistry factory's pool list
  (34,187 indexes on Base today), filtering dead gauges — a short page
  does NOT mean the end of the list, and `Voter.length()` (1,816) is
  the WRONG total (first attempt found only 16 ancient pools).
  `AERODROME_FACTORY_REGISTRY` added to known_contracts for this.
- LpEpoch decode is sanity-checked live: the running epoch's `ts` must
  equal the Thursday-anchored epoch start, else abort — that's the
  guard against hand-derived-ABI drift (RewardsSugar is unverified).
- Epoch mechanics (docs + SPECIFICATION.md): epoch N's fees +
  incentives pay the votes standing at N's close, so voting late loses
  nothing; voting is disabled in the final hour — practical cutoff
  Wednesday 23:00 UTC.
- Full scan ≈ 3.5 min on the free Alchemy tier (~170 chunked
  `eth_call`s + per-candidate deep looks). Fine for a probe; the
  service version should cache the alive-gauge set between runs.
- Verified live 2026-07-07: 758 alive gauges; suggestions were
  CL USDC/cbBTC tiers, CL100-WETH/USDT, CL200-AERO/cbBTC at ~$44–69
  per epoch for 33k votes — plausible against the site's vote page.

- Promoted same-day into the service as `hrusha/service/vote_scout.py`
  + a `/votes` dashboard page: pure scoring (`score_pool`/`rank`,
  offline-tested) split from the chain scan (`scan`, background thread
  behind a "scan pools" button, mirroring the refresh pattern — CSRF
  Origin check on POST, one scan at a time, errors reported by class
  name only so RPC URLs/keys never reach the page). Personal returns
  render in percent (%/epoch + APR on vote-power value at AERO spot)
  per operator preference; pool names are chain-derived and rely on
  Jinja autoescape (tested); a stale-scan banner appears when the
  result predates the running epoch. The page doubles as the
  voted-this-epoch indicator (per-veNFT pills from VeSugar).

- Back-test (docs/examples/vote_backtest_probe.py, run 2026-07-07 over
  the 5 then-suggested pools, 12 completed epochs, rewards valued at
  claim-day DefiLlama prices): realized $/1k votes stayed in a
  $1.0–1.9 band with medians $1.28–1.40; walk-forward prediction error
  (median-of-prior-6, same model as the live scout) was 15–31% median
  absolute — calibrated, no promised-$2-paid-$0.10 collapses. Known
  bias: the pool set was chosen by TODAY's scan (survivorship); the
  clean fix is forward tracking — persist each scan's suggestions and
  compare against realized epochs, which the Phase 5b scheduler can do
  for free.

- Vote report card (docs/examples/vote_history_probe.py): reconstructs
  the operator's standing votes at every epoch close from the Voter's
  `Voted`/`Abstained` events, bounded to veNFT OWNERSHIP WINDOWS from
  the ledger's ERC-721 legs (the operator trades veNFTs — a sold NFT's
  last vote otherwise "stands" forever and inflates history), then
  scores each pick against the scout's walk-forward target. Run
  2026-07-07: 84 judged pool-epochs, 80% hit rate, realized 104% of
  target in aggregate; ~13 pool-epochs unknowable because the pool no
  longer answers `epochsByAddress` (likely killed gauges — votes
  parked there may have earned nothing; a killed-gauge flag belongs in
  the scout). Two data-source gotchas cost an afternoon: HexBytes
  `.hex()` returns WITHOUT the 0x prefix and Blockscout silently
  ignores the malformed topic0 filter (Abstained rows then decode as
  votes = double counting); and a CORRECT server-side topic0 filter
  forces Blockscout onto a query plan that times out — filter by
  topic1 (the rare key) and classify topic0 client-side.

- Filter lab (docs/examples/pool_filter_lab.py, run 2026-07-07 over 112
  pools × 18 epochs, rewards at claim-day prices): the gates are now
  operator-tunable (config `vote_scout` section) and the lab's evidence
  reshaped the recommendations. (1) TVL correlates only WEAKLY with
  payout stability (Spearman −0.22 vs CV) and NEGATIVELY with returns
  (−0.32), but the TVL≥300k gate still won the walk-forward simulation
  outright ($810 vs $671 ungated over 12 epochs at the operator's
  power) because it excludes the predicted-great-paid-zero traps
  (ungated's worst week realized $0). (2) The majors-only pair gate
  adds NO stability (exotic CV 0.23 vs majors 0.22, same ratio/p10)
  and COSTS money on top of TVL (both-gates $552 vs TVL-only $810) —
  exotic pools won 12/12 simulated weeks; default stays shipped but
  the operator's evidence-backed config is require_major_pair: false.
  (3) Young tokens are not toxic to returns but ARE volatile: 30–90d
  tokens' payout CV 0.55 vs 0.23 for older (age–CV Spearman −0.35 in
  exotics), so a min_token_age_days: 90 gate (new, off by default)
  buys predictability at zero simulated cost. Oracle ceiling was $1,726
  — the model captures roughly half of hindsight-perfect picking.
  Caveats: current TVL credited to past epochs; survivorship (universe
  = pools paying today) understates small-pool graveyard risk.

- Scan concurrency: attempted and REVERTED same day (operator call —
  "if it works it works"; the sequential 3.5-min scan is reliable and
  the added machinery wasn't worth it for a weekly personal tool). The
  measurements stand for whoever revisits: the scan is 96-98% network
  wait (~8s CPU); Alchemy's free tier meters ~330 CU/s (eth_call 26 CU
  ≈ 12.7 calls/s), so unpaced workers 429 regardless of backoff — the
  working shape was a global pacer at 10 req/s (12 rps calibrated
  clean) plus Multicall3 batching of the ~800 tiny reads, which
  delivered a verified 29s full scan with identical results. The code
  was reverted before ever being committed, so this paragraph IS the
  recipe if scan latency ever matters (scheduler pre-cutoff runs):
  thread-local Web3 per worker, retries only on transport errors so
  spam-token reverts don't burn retry cycles, Multicall3 at the
  canonical cross-chain address via aggregate3 with allowFailure.

## Follow-ups

- CLI twin (`hrusha suggest-votes`) if terminal use ever wants it; the
  scheduler (Phase 5b) should auto-scan Wednesday evenings before the
  cutoff and could alert when suggestions change late.
- Persist scan snapshots (suggestions + projections per epoch) so
  forward performance is measurable without survivorship bias.
- Killed-gauge detection: flag suggested pools whose gauge dies, and
  alert if the operator's standing votes point at a dead gauge.
- Historical rewards in `epochsByAddress` are valued at TODAY's token
  prices (spot endpoint); good enough for vote-weight projection, but
  a service version could reuse the ledger's daily price cache.
- Vote-splitting optimizer (spread power across top N pools until
  marginal $/vote equalizes) — the math is there, the UX isn't.
