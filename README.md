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
hrusha sync              # full sync: transfers, gas fees, balance snapshots -> SQLite
hrusha balances          # live token balances with USD values
hrusha transfers         # recent transfers from the ledger, with tags
hrusha fees --days 30    # gas spent over a window (includes Base L1 data fee)
```
Sync is incremental and idempotent: a per-address block cursor plus a
dedup constraint make re-runs and overlaps harmless. Transfers between
your own tracked addresses are auto-tagged `own-transfer` and excluded
from income/spend. The epoch/source reports and the dashboard arrive
with Phases 2–5 of [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

Via Docker:
```bash
docker build -t hrusha .
docker run --rm -v ~/.hrusha:/config:ro hrusha sync --dry-run
```

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
