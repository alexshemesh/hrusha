[![Lint And Test](https://github.com/alexshemesh/hrusha/actions/workflows/test.yml/badge.svg)](https://github.com/alexshemesh/hrusha/actions/workflows/test.yml)
# hrusha

Personal crypto income monitor on Base: track token balances, transfers
in/out, gas fees, and neto profit per income source (Aerodrome voting,
Morpho, 40acres) across several addresses.

Data providers: **Blockscout** (transfer history — free, no key),
**DefiLlama** (historical USD prices — free, no key) and **Alchemy**
(balances, exact gas fees incl. the Base L1 data fee, price fallback —
free tier). PnL is computed locally. See [docs/DESIGN.md](docs/DESIGN.md)
for the design, [docs/design-logs/](docs/design-logs/INDEX.md) for why
the providers changed, and
[docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) for the
phased build plan.

# Development setup
Requires Python 3.12+ (`brew install python@3.12`) and
[gitleaks](https://github.com/gitleaks/gitleaks) for the pre-commit secret
scan (`brew install gitleaks`).

```bash
make venv                      # create .venv with python3.12
source .venv/bin/activate
make prepare                   # pip install -e '.[dev]'
make hooks                     # install the gitleaks pre-commit hook — do not skip
```

# Configuration
Configuration lives in `~/.hrusha/config.yaml` — **created manually,
outside the repo, never committed**. The CLI refuses to start without it
and tells you exactly what is missing (without echoing values).
```yaml
addresses:
  main: "0xYourFirstAddress"
  cold: "0xYourSecondAddress"

alchemy:
  api_key: "your alchemy key"

etherscan:
  api_key: "your etherscan key"

# optional; defaults to ~/.hrusha/hrusha.db (the container sets /data/hrusha.db)
# db_path: "~/.hrusha/hrusha.db"

# optional: vote-scout risk gates (dashboard /votes page); defaults shown.
# docs/examples/pool_filter_lab.py re-derives these from realized epoch data
# vote_scout:
#   min_tvl_usd: 300000
#   require_major_pair: true       # false = exotic pairs may be suggested
#   extra_major_symbols: []        # e.g. [REI] to trust beyond the builtin set
#   max_vote_cv: 0.6
#   min_fee_share: 0.10
#   min_history: 3
#   min_token_age_days: 0        # >0 flags pools whose youngest token is newer
#   min_fees_per_emission: 0.1  # informational note on pools earning less in
#                                # fees than this fraction of emitted AERO
#   token_safety: true           # GoPlus honeypot/tax/pausable checks on
#                                # non-major pair + bribe tokens

# later, for the Sheets export:
# sheets:
#   spreadsheet_id: "..."
#   service_account_file: "~/.hrusha/service-account.json"
```
Override the config location with the `HRUSHA_CONFIG` environment
variable (the Docker image sets it to `/config/config.yaml`).

# Usage
```bash
hrusha sync --dry-run    # read config, connect to Alchemy, print ETH balances
hrusha sync              # full sync: transfers, fees, tagging, snapshots -> SQLite
hrusha balances          # live token balances with USD values
hrusha transfers         # recent transfers from the ledger, with ids, sources, tags
hrusha fees --days 30    # gas spent over a window (includes Base L1 data fee)
hrusha report --days 90  # neto per epoch x source (--coins for native amounts)
hrusha tag 123 bribe     # manually tag event 123 (manual always beats rules)
hrusha retag             # re-run tag rules + epoch assignment over the ledger
```
Sync is incremental and idempotent: a per-address block cursor plus a
dedup constraint make re-runs and overlaps harmless. Transfers between
your own tracked addresses are auto-tagged `own-transfer` and excluded
from income/spend. Events are grouped into Aerodrome epochs (weekly
flip, Thu 00:00 UTC); tag rules assign tags and an income source, and
manual tags always survive re-tagging. Protocol adapters and the web
dashboard arrive with Phases 3–5 of
[docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

# Deployment (Docker)
One command per lifecycle step, built locally from the checkout — see
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the full runbook (LAN
exposure, backup/restore, troubleshooting). Bare-metal `hrusha serve`
stays fully supported; the container is just packaging.
```bash
./deploy/deploy.sh    # build + start; dashboard on http://127.0.0.1:8787
./deploy/status.sh    # health, ledger size, recent errors
./deploy/backup.sh    # consistent SQLite backup (keeps newest 14)
./deploy/upgrade.sh   # backup -> git pull -> rebuild -> health -> doctor
```
Config mounts read-only from `~/.hrusha/config.yaml`; the ledger and
JSON logs live in `deploy/data/` and `deploy/logs/` on the host. An
AI-operations runbook ships in the repo at
`.claude/skills/hrusha-ops/` so a Claude session on any machine can
deploy and maintain the service.

# Tests & lint
```bash
make test     # pytest
make lint     # ruff check + format check
make leaks    # full-history gitleaks scan
```

# Security
The repo is public; private data never enters it:
- addresses and API keys live only in `~/.hrusha/config.yaml`
- `.gitignore` blocks `config.yaml`, databases, `.env*`, service-account files
- a gitleaks pre-commit hook (`make hooks`) and a CI job scan every change
- error messages and logs never echo config values
