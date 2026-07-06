"""Phase 3 spike: can Aerodrome's Sugar contracts answer our questions?

Read-only probe against the real wallet — no SQLite, no sync, no state.
It must show, per tracked address:
  1. veNFT ids + lock size/expiry
  2. votes cast this epoch ("have I voted yet")
  3. claimable bribes + fees per gauge (the night-before-epoch view)

Requires web3 (spike-only dependency): .venv/bin/pip install web3
Run:  .venv/bin/python docs/examples/aerodrome_probe.py

Contract addresses come from the official deployments file
(github.com/velodrome-finance/sugar, deployments/base.env); Voter and
VeSugar identities were confirmed against Blockscout verified sources.
VeSugar's ABI is fetched live from Blockscout; RewardsSugar is not
source-verified anywhere public, so its minimal ABI below was derived
by hand from contracts/RewardsSugar.vy in the sugar repo.

Fun fact confirming Phase 2: RewardsSugar computes epochs as
`block.timestamp // WEEK * WEEK` — the same Thursday-anchored math as
hrusha.ledger.tags.epoch_id_for().
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import httpx
from web3 import Web3

# public contract addresses live in hrusha/adapters/ — the only path the
# gitleaks pre-commit hook allowlists for raw 0x addresses
from hrusha.adapters.known_contracts import REWARDS_SUGAR, VE_SUGAR
from hrusha.config import load_config

BLOCKSCOUT_ABI_URL = "https://base.blockscout.com/api/v2/smart-contracts/{address}"
SECONDS_PER_WEEK = 604_800
POOLS_PER_CALL = 300  # rewards() scans pools; chunk to keep eth_call cheap
MAX_POOL_CHUNKS = 12

# hand-derived from contracts/RewardsSugar.vy (struct Reward + def rewards)
REWARDS_SUGAR_ABI = [
    {
        "name": "rewards",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "_limit", "type": "uint256"},
            {"name": "_offset", "type": "uint256"},
            {"name": "_venft_id", "type": "uint256"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "venft_id", "type": "uint256"},
                    {"name": "lp", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                    {"name": "token", "type": "address"},
                    {"name": "fee", "type": "address"},
                    {"name": "bribe", "type": "address"},
                ],
            }
        ],
    }
]

ERC20_ABI = [
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]


def day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def fetch_verified_abi(address: str) -> list:
    response = httpx.get(BLOCKSCOUT_ABI_URL.format(address=address), timeout=30)
    response.raise_for_status()
    abi = response.json().get("abi")
    if not abi:
        raise SystemExit(f"no verified ABI on Blockscout for {address}")
    return abi


def main() -> None:
    config = load_config()
    w3 = Web3(Web3.HTTPProvider(f"https://base-mainnet.g.alchemy.com/v2/{config.alchemy_api_key}"))
    print(f"connected: chain_id={w3.eth.chain_id}, block={w3.eth.block_number}")

    ve_sugar = w3.eth.contract(
        address=Web3.to_checksum_address(VE_SUGAR), abi=fetch_verified_abi(VE_SUGAR)
    )
    rewards_sugar = w3.eth.contract(
        address=Web3.to_checksum_address(REWARDS_SUGAR), abi=REWARDS_SUGAR_ABI
    )
    epoch_start = int(time.time()) // SECONDS_PER_WEEK * SECONDS_PER_WEEK
    token_info: dict[str, tuple[str, int]] = {}

    def describe_token(address: str) -> tuple[str, int]:
        if address not in token_info:
            erc20 = w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC20_ABI)
            try:
                token_info[address] = (
                    erc20.functions.symbol().call(),
                    erc20.functions.decimals().call(),
                )
            except Exception:  # noqa: BLE001 — spam/odd tokens: show the address instead
                token_info[address] = (address[:10], 18)
        return token_info[address]

    for label, wallet in config.addresses.items():
        print(f"\n== {label} ({wallet})")
        venfts = ve_sugar.functions.byAccount(Web3.to_checksum_address(wallet)).call()
        if not venfts:
            print("   no veNFTs")
            continue
        # VeSugar.byAccount returns VeNFT structs; field order per verified ABI
        fields = [
            c["name"]
            for c in ve_sugar.get_function_by_name("byAccount").abi["outputs"][0]["components"]
        ]
        for raw in venfts:
            nft = dict(zip(fields, raw, strict=True))
            amount = nft["amount"] / 1e18
            voted_this_epoch = nft["voted_at"] >= epoch_start
            expires = day(nft["expires_at"]) if nft["expires_at"] else "permanent"
            print(
                f"   veNFT #{nft['id']}: locked {amount:,.2f} AERO, expires {expires}, "
                f"governance power {nft['voting_amount'] / 1e18:,.2f}"
            )
            print(
                f"   voted this epoch (since {day(epoch_start)}): "
                f"{'YES' if voted_this_epoch else 'no'} "
                f"(last vote {day(nft['voted_at']) if nft['voted_at'] else 'never'})"
            )
            for vote in nft["votes"]:
                lp, weight = vote
                print(f"      vote: pool {lp} weight {weight / 1e18:,.2f}")

            print("   claimable rewards (bribes + fees per gauge):")
            found = 0
            for chunk in range(MAX_POOL_CHUNKS):
                rewards = rewards_sugar.functions.rewards(
                    POOLS_PER_CALL, chunk * POOLS_PER_CALL, nft["id"]
                ).call()
                for reward in rewards:
                    _, lp, amount_raw, token, fee, bribe = reward
                    symbol, decimals = describe_token(token)
                    kind = "fee" if fee != "0x" + "0" * 40 else "bribe"
                    print(
                        f"      {symbol:<12} {amount_raw / 10**decimals:>18,.6f}  "
                        f"({kind}, pool {lp[:10]}...)"
                    )
                    found += 1
            if not found:
                print("      none right now")

    print("\nepoch flips next:", day(epoch_start + SECONDS_PER_WEEK))


if __name__ == "__main__":
    main()
