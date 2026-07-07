---
name: hrusha-ops
description: Operate, deploy, upgrade and troubleshoot the hrusha crypto income monitor (Docker compose deployment, SQLite ledger, Aerodrome vote scout). Use when asked to deploy, upgrade, back up, restore, diagnose, or run maintenance on hrusha — on this machine or the deployment box.
---

# hrusha operations runbook

hrusha is a personal crypto income monitor on Base (public repo,
security-paramount). One FastAPI dashboard + CLI over a local SQLite
ledger. It deploys via Docker compose built from a repo checkout —
no registry, no remote state.

## Hard rules — never violate

1. **Never commit or echo config values.** Addresses and API keys live
   only in `~/.hrusha/config.yaml` (0600, gitignored). Error paths in
   this codebase deliberately print class names, not messages, because
   provider exceptions embed the Alchemy API key in URLs.
2. **The dashboard has no auth.** Default bind is 127.0.0.1. Exposing
   beyond localhost is the operator's explicit decision only
   (`HRUSHA_BIND`), never a side effect of a fix.
3. **Back up before anything that touches the ledger schema or file**
   (`deploy/backup.sh`; upgrade.sh already does).
4. Raw hex addresses in code belong ONLY in `hrusha/adapters/` —
   the gitleaks pre-commit hook enforces this; never widen its
   allowlist.
5. New external APIs start as read-only probes in `docs/examples/`.

## Where things live

| what | bare-metal | Docker host | container |
|---|---|---|---|
| config | `~/.hrusha/config.yaml` | same, mounted ro | `/config/config.yaml` |
| ledger | `~/.hrusha/hrusha.db` | `deploy/data/hrusha.db` | `/data/hrusha.db` (via `HRUSHA_DB_PATH`) |
| manual-tag backup | `~/.hrusha/rules.yaml` | same | import via `docker compose cp` |
| JSON logs | stderr (+`HRUSHA_LOG_DIR`) | `deploy/logs/hrusha.jsonl` | `/logs/` |
| UI | `http://127.0.0.1:8787` | same (or `HRUSHA_BIND`) | `:8787` |

## Operations

- Deploy / redeploy: `./deploy/deploy.sh` (build from checkout, up, health-wait)
- Upgrade: `./deploy/upgrade.sh` (backup → `git pull --ff-only` → rebuild →
  health → doctor). Schema migrations are automatic on open.
- Backup: `./deploy/backup.sh` (online-consistent, keeps newest 14)
- Status: `./deploy/status.sh`; live logs: `docker compose logs -f hrusha`
- Any CLI command in the container: `docker compose exec hrusha hrusha <cmd>`
- Rollback: `docker compose down`, copy a backup over
  `deploy/data/hrusha.db`, `git checkout <prior-sha>`, `./deploy/deploy.sh`
- Full details: `docs/DEPLOYMENT.md`

## The data-integrity pipeline (run in this order)

1. **sync** — dashboard refresh button or `hrusha sync`. Incremental,
   idempotent.
2. **doctor** — reconciles ledger vs live chain balances (exit 5 =
   mismatch). Known noise: spam tokens, ~0.02 ETH internal-transactions
   gap, sub-$1 dust.
3. **heal** — repairs Blockscout indexer gaps (chronic; drops whole
   transactions) by binary-searching archive balances and ingesting
   missing legs from raw receipts. Run only when doctor shows real
   token mismatches after a fresh sync.
4. **reprice** — backfills USD on unpriced legs after the price cache
   was poisoned by throttling. Run when income/put-in numbers look
   too low and `unpriced` counts are high.

## Vote scout (the /votes page)

- Epochs flip Thu 00:00 UTC; voting is disabled the final hour —
  practical cutoff **Wed 23:00 UTC**. Scan late (Wed evening) for the
  best information; a full scan takes ~3.5 min.
- Gates come from the `vote_scout` config section (see README).
  Solid pills block a pool from "suggested"; muted pills
  (EMISSIONS-SUBSIDIZED, SELF-BRIBED) are informational by operator
  decision.
- "GoPlus unreachable" banner = token-safety flags missing this scan;
  rescan before trusting exotic suggestions.
- Never present displayed vAPR as expected return — the scout's
  dilution-adjusted projection exists because vAPR ignores late votes.

## Failure signatures

| signature | cause | fix |
|---|---|---|
| doctor exit 5 after sync | Blockscout gaps | `hrusha heal`, re-run doctor |
| unpriced legs pile up | DefiLlama throttling poisoned cache | `hrusha reprice` |
| balances missing real tokens | Alchemy Portfolio pagination | already fixed; re-sync |
| scan fails HTTPError | Alchemy 429 (rate limit) | retry; scan is sequential on purpose (concurrency was tried, measured 29s, reverted — recipe in design log 2026-07-07-1126) |
| `extra={"name": ...}` crash in logs | reserved LogRecord field | rename the key (e.g. vault_name) |
| Blockscout topic query hangs | server-side topic0 filter is slow | filter by topic1, classify client-side |

## Conventions when changing code

- Run `make lint` (ruff check AND format check — CI enforces both) and
  `make test` before any commit; commit/push only when the operator asks.
- Work on phase branches; the operator merges PRs to main themselves.
- Strong-signal changes get a design log entry in `docs/design-logs/`
  (see the design-logs convention; INDEX.md lists all entries — read
  the relevant ones before re-deriving decisions recorded there).
