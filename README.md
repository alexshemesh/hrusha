[![Lint And Test](https://github.com/alexshemesh/hrusha/actions/workflows/tets.yml/badge.svg)](https://github.com/alexshemesh/hrusha/actions/workflows/tets.yml)
# hrusha

Personal ETH portfolio monitor: track token balances, transfers in/out, gas
fees, and profits across several Ethereum addresses.

Data providers: **Alchemy** (balances + USD prices + transfers, free tier)
and **Etherscan** (exact gas-fee accounting, free tier). PnL is computed
locally. See [docs/DESIGN.md](docs/DESIGN.md) for the full design and the
provider comparison (DeBank, Alchemy, MetaMask/Infura, Zerion, Moralis,
Etherscan), and [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)
for the phased build plan.

# Dependencies
- Python 3.8 or higher. See installation [instructions here](https://www.python.org/downloads/)
- Use python virtual environments
```
# Create virtual env
python -m venv ~/.env/hrusha
# Activate virtual env
source ~/.env/hrusha/bin/activate
```

- install dependencies
```
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

# Configuration
Configuration lives in `~/.hrusha/config.yaml` — **created manually,
outside the repo, never committed**. The service refuses to start without
it and tells you exactly what is missing.
```yaml
addresses:
  main: "0xYourFirstAddress"
  cold: "0xYourSecondAddress"

alchemy:
  api_key: "your alchemy key"

etherscan:
  api_key: "your etherscan key"

# later, for the Sheets export:
# sheets:
#   spreadsheet_id: "..."
#   service_account_file: "~/.hrusha/service-account.json"
```

# Tests
Tests are regular pytest set. Read here [more](https://docs.pytest.org/en/7.1.x/)</br>
```
pytest .
```

# Execute
The service CLI (`hrusha sync`, `hrusha balances`, ...) arrives with
Phase 0/1 of [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).
Nothing runnable ships yet.
