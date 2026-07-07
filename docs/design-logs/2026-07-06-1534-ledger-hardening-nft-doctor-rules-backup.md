---
date: 2026-07-06T15:34
type: architecture
status: merged
trigger: cross-cutting
touches:
  - hrusha/providers/blockscout.py
  - hrusha/providers/interface.py
  - hrusha/ledger/store.py
  - hrusha/ledger/ingest.py
  - hrusha/ledger/rules_io.py
  - hrusha/service/sync.py
  - hrusha/service/doctor.py
  - hrusha/service/heal.py
  - hrusha/cli.py
related:
  - 2026-07-06-1117-blockscout-defillama-providers.md
supersedes: null
commit: b1f8efa
pr: https://github.com/alexshemesh/hrusha/pull/9
---

# Hardening pass: ERC-721 ingestion, doctor/heal reconciliation, local-rules backup

## Context

Phases 1–4 proved the transfer-ledger architecture on real data, and the
same live usage exposed three wounds:

1. veNFT purchases (pay ERC-20, receive an ERC-721) looked like tokens
   vanishing to strangers, because the ledger only ingested ERC-20 and
   native transfers. The operator had to correct the accounting by hand.
2. A 40acres position analysis showed net vault-share flows exceeding the
   on-chain `balanceOf` — with no way to tell whether money or data was
   missing. There was no automatic "does the ledger agree with the
   chain?" check in a tool whose whole job is counting money.
3. Hand-added tag rules (personal swap counterparties, protocols without
   adapters) and manual tags live only in the SQLite DB, which is
   otherwise derived state and was already wiped once during development.
   A DB reset silently destroys hand-curated accounting knowledge.

## Problem / Goal

Close the model gap (NFTs are first-class portfolio events), make ledger
completeness checkable by machine, and make local knowledge survive DB
resets — before building the Phase 5 dashboard on top of the data.

## Decision

- **ERC-721 ingestion** (schema v3: `events.token_id`): Blockscout's
  `tokennfttx` becomes a second fetch with its own sync cursor
  (`nft_cursor:<address>`), so ledgers that predate the feature backfill
  NFT history without resetting the main cursor. NFT synthetic ordinals
  start at 100 000 so they can never collide with ERC-20 ordinals in the
  same tx on `UNIQUE(tx_hash, log_index, kind)`. NFTs are never priced
  (`usd_at_time` stays NULL); the existing same-tx swap detector now sees
  purchase txs (tokens out + NFT in) and classifies them as non-flow.
- **`hrusha doctor`**: reconciles, per (address, token), net ledger flows
  against live chain state — ERC-20 sums and ERC-721 counts vs
  `balanceOf`, native ETH net-minus-gas vs `eth_getBalance` (with a small
  tolerance for reverted-tx gas the ledger cannot see). Chain reads are
  injected callables, so the logic is unit-testable offline. Exit code 5
  signals discrepancies for scripting.
- **`hrusha heal`**: repairs the gaps doctor finds, per (address, token)
  the operator has actually sent (spam airdrops never qualify). It
  replays the ledger's legs as an expected-balance step function,
  binary-searches archive `balanceOf` for the first divergent block,
  reads that block's Transfer logs straight from the RPC node, and
  ingests the missing legs — priced, tagged, epoch-assigned, and with
  the missing tx's gas fee. Healed legs carry their *real* chain log
  index; a content-level dedup (tx, kind, address, contract, amount,
  token_id) protects partially-indexed txs from double entry. Heal
  never probes past the sync cursor of the token's family — beyond it
  is sync's territory, and healing there would duplicate legs when sync
  later fetches the same tx under a synthetic ordinal. Balance moves
  with no Transfer logs are reported as unexplained, never guessed at.
  Known limit: missing transfers that exactly cancel within one probe
  interval are invisible to balance probes.
- **`hrusha rules export|import`**: backs up all tag rules plus manual
  tags to `~/.hrusha/rules.yaml` (0600, private like config.yaml — it
  names personal counterparties and must never enter the repo). Manual
  tags are keyed by `(tx_hash, log_index, kind)`, not event id, so they
  survive a full DB rebuild. Import is idempotent.

## Alternatives Considered

- **Full balance reconciliation only for known protocol contracts** —
  rejected: the point is catching *unknown* gaps; restricting the check
  to contracts we already model defeats it. Spam-token noise is the
  accepted cost (their diffs are self-inflicted balance manipulation and
  easy to ignore).
- **Exporting only non-seed, non-discovered rules** — rejected: telling
  local rules apart from seeded/discovered ones is fragile bookkeeping;
  exporting everything and relying on idempotent import is simpler and
  loses nothing.
- **Storing NFT transfers in a separate table** — rejected: they are
  transfers; one extra nullable column keeps every existing query,
  tagging rule, and report working on them for free.
- **Do nothing until the dashboard** — rejected: the dashboard would
  render numbers we now know can silently be wrong.

## Implementation Notes

- First live `doctor` run immediately paid for the feature: it confirmed
  the 40acres share gap (ledger 2.87 vs chain 2.06 shares) and receipt-
  level digging proved the cause is **missing transactions in
  Blockscout's Base index** — a legitimate vault withdrawal (share burn +
  USDC payout) absent from both `tokentx` and Blockscout's own `getLogs`
  API, while the raw RPC receipt shows it plainly. The apparent 40acres
  "loss" was a data gap, not money. Several smaller diffs (two veNFT
  burns, some USDC outflows, small ETH/WETH amounts) point to more
  unindexed txs.
- Consequence for the provider design log (2026-07-06-1117): Blockscout
  is keyless and generous but **not complete** — and not just once: a
  same-day sync also silently missed several fresh transactions, so
  the gaps are chronic, not a March incident. Doctor + heal is the
  standing countermeasure.
- First live heal repaired 19 gaps (22 transfers + 7 fees): one whole
  protocol-restructuring day the index had dropped, plus scattered
  singles. After healing, every real token reconciles exactly; the
  remaining doctor noise is spam tokens, dust, and a small native-ETH
  gap from internal transactions (txlist is top-level only).
- `_compare` in doctor passes `token` positionally once because ruff
  S106 mistakes `token="ETH"` for a hardcoded secret.

## Follow-ups

- Internal-transaction ETH ingestion (`txlistinternal`): the remaining
  native-ETH doctor gap; heal deliberately does not cover native ETH.
- Content-level dedup in *sync* ingestion, so an indexer that later
  backfills a healed tx (synthetic ordinal vs real log index) cannot
  double-enter it. Today the cursor makes this unlikely, not impossible.
- Spam-token noise in doctor output could use a suppression heuristic
  (e.g. tokens never sent by the operator).
