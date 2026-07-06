"""Aerodrome ve(3,3) adapter: positions, claimables, and claim recognition.

Three jobs (docs/IMPLEMENTATION_PLAN.md, Phase 3):
- veNFT positions per wallet (lock size/expiry, voted-this-epoch) via the
  VeSugar helper — one eth_call per wallet.
- Claimable bribes + fees per gauge via RewardsSugar — the night-before-
  epoch view, written as `claimable` snapshots.
- Recognizing historical claims WITHOUT scanning event logs: every claim
  is already in the ledger as a transfer_in whose counterparty is an
  Aerodrome reward contract. Reward contracts all expose `voter()`
  returning the canonical Voter, so a one-time eth_call per distinct
  counterparty separates them from everything else; verified ones become
  tag rules (source `aerodrome-voting`), and verdicts are cached in
  sync_state so each counterparty is checked exactly once, ever.

ABIs are vendored minimal fragments: VeSugar's is copied verbatim from
its Blockscout-verified source; RewardsSugar is not verified anywhere
public, so its fragment was hand-derived from contracts/RewardsSugar.vy
(github.com/velodrome-finance/sugar). Sugar addresses live in
known_contracts.py — the gitleaks-allowlisted home for public contracts.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from web3 import Web3
from web3.exceptions import Web3Exception

from hrusha.adapters.known_contracts import (
    AERODROME_VOTER,
    REWARDS_SUGAR,
    SOURCE_AERODROME,
    VE_SUGAR,
)
from hrusha.ledger.tags import CLAIM_TAG, ensure_rule

AERO_DECIMALS = 18
POOLS_PER_CALL = 300  # RewardsSugar.rewards scans pools; chunked eth_calls
MAX_POOL_CHUNKS = 12
REWARD_CONTRACT_VERDICT_KEY = "aero_reward_contract:{address}"
CLAIM_RULE_PRIORITY = 50  # ahead of the seeded token-based guesses (100)

log = logging.getLogger("hrusha.adapters.aerodrome")

# verbatim from VeSugar's Blockscout-verified ABI (byAccount only)
VE_SUGAR_ABI = [
    {
        "name": "byAccount",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "_account", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple[]",
                "components": [
                    {"name": "id", "type": "uint256"},
                    {"name": "account", "type": "address"},
                    {"name": "decimals", "type": "uint8"},
                    {"name": "amount", "type": "uint128"},
                    {"name": "voting_amount", "type": "uint256"},
                    {"name": "governance_amount", "type": "uint256"},
                    {"name": "rebase_amount", "type": "uint256"},
                    {"name": "expires_at", "type": "uint256"},
                    {"name": "voted_at", "type": "uint256"},
                    {
                        "name": "votes",
                        "type": "tuple[]",
                        "components": [
                            {"name": "lp", "type": "address"},
                            {"name": "weight", "type": "uint256"},
                        ],
                    },
                    {"name": "token", "type": "address"},
                    {"name": "permanent", "type": "bool"},
                    {"name": "delegate_id", "type": "uint256"},
                    {"name": "managed_id", "type": "uint256"},
                ],
            }
        ],
    }
]

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

VOTER_GETTER_ABI = [
    {
        "name": "voter",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    }
]

ERC20_DECIMALS_ABI = [
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    }
]


@dataclass(frozen=True)
class VeNft:
    id: int
    locked_aero: Decimal
    voting_amount: Decimal
    rebase_aero: Decimal  # pending rebase; claims auto-compound into the lock
    expires_at: int  # 0 for permanent locks
    voted_at: int
    permanent: bool
    votes: tuple[tuple[str, Decimal], ...]  # (pool, weight)


@dataclass(frozen=True)
class Claimable:
    venft_id: int
    pool: str
    token: str  # token contract, lowercase
    amount: Decimal  # scaled by the token's decimals
    is_fee: bool  # False = bribe


class AerodromeAdapter:
    """Read-only contract views over Aerodrome, one Web3 client."""

    def __init__(self, w3: Web3) -> None:
        self._w3 = w3
        self._ve_sugar = w3.eth.contract(
            address=Web3.to_checksum_address(VE_SUGAR), abi=VE_SUGAR_ABI
        )
        self._rewards_sugar = w3.eth.contract(
            address=Web3.to_checksum_address(REWARDS_SUGAR), abi=REWARDS_SUGAR_ABI
        )
        self._decimals_cache: dict[str, int] = {}

    def venfts(self, address: str) -> list[VeNft]:
        raw = self._ve_sugar.functions.byAccount(Web3.to_checksum_address(address)).call()
        return [_venft_from_tuple(item) for item in raw]

    def claimables(self, venft_id: int) -> list[Claimable]:
        found: list[Claimable] = []
        for chunk in range(MAX_POOL_CHUNKS):
            rewards = self._rewards_sugar.functions.rewards(
                POOLS_PER_CALL, chunk * POOLS_PER_CALL, venft_id
            ).call()
            for _venft, lp, amount_raw, token, fee, _bribe in rewards:
                token = token.lower()
                found.append(
                    Claimable(
                        venft_id=venft_id,
                        pool=lp.lower(),
                        token=token,
                        amount=Decimal(amount_raw) / Decimal(10) ** self._token_decimals(token),
                        is_fee=int(fee, 16) != 0,
                    )
                )
        return found

    def is_reward_contract(self, address: str) -> bool:
        """True when `address` is an Aerodrome bribe/fee reward contract."""
        contract = self._w3.eth.contract(
            address=Web3.to_checksum_address(address), abi=VOTER_GETTER_ABI
        )
        try:
            voter = contract.functions.voter().call()
        except (Web3Exception, ValueError):
            return False  # EOAs and unrelated contracts revert or decode garbage
        return voter.lower() == AERODROME_VOTER.lower()

    def _token_decimals(self, token: str) -> int:
        if token not in self._decimals_cache:
            contract = self._w3.eth.contract(
                address=Web3.to_checksum_address(token), abi=ERC20_DECIMALS_ABI
            )
            try:
                self._decimals_cache[token] = contract.functions.decimals().call()
            except (Web3Exception, ValueError):
                self._decimals_cache[token] = AERO_DECIMALS
        return self._decimals_cache[token]


def discover_claim_rules(conn: sqlite3.Connection, adapter: AerodromeAdapter) -> int:
    """Turn reward-contract counterparties into claim tag rules. Returns rules added.

    Each distinct transfer_in counterparty is checked against the chain at
    most once ever (verdict cached in sync_state); confirmed reward
    contracts get a rule tagging their transfers `claim` with source
    aerodrome-voting.
    """
    counterparties = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT counterparty FROM events"
            " WHERE kind = 'transfer_in' AND counterparty IS NOT NULL"
        )
    ]
    rules_added = 0
    for address in counterparties:
        verdict = _cached_verdict(conn, address)
        if verdict is None:
            verdict = adapter.is_reward_contract(address)
            _store_verdict(conn, address, verdict)
        if verdict and _ensure_claim_rule(conn, address):
            rules_added += 1
            log.info("aerodrome reward contract found", extra={"counterparty": address})
    return rules_added


def _cached_verdict(conn: sqlite3.Connection, address: str) -> bool | None:
    row = conn.execute(
        "SELECT value FROM sync_state WHERE key = ?",
        (REWARD_CONTRACT_VERDICT_KEY.format(address=address),),
    ).fetchone()
    return None if row is None else row[0] == "1"


def _store_verdict(conn: sqlite3.Connection, address: str, verdict: bool) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO sync_state (key, value) VALUES (?, ?)",
            (REWARD_CONTRACT_VERDICT_KEY.format(address=address), "1" if verdict else "0"),
        )


def _ensure_claim_rule(conn: sqlite3.Connection, counterparty: str) -> bool:
    """Add the claim rule for a reward contract unless it already exists."""
    match = {"counterparty": counterparty, "direction": "in"}
    return ensure_rule(conn, CLAIM_RULE_PRIORITY, match, [CLAIM_TAG], SOURCE_AERODROME)


def _venft_from_tuple(raw: tuple) -> VeNft:
    fields = [c["name"] for c in VE_SUGAR_ABI[0]["outputs"][0]["components"]]
    nft = dict(zip(fields, raw, strict=True))
    scale = Decimal(10) ** AERO_DECIMALS
    return VeNft(
        id=nft["id"],
        locked_aero=Decimal(nft["amount"]) / scale,
        voting_amount=Decimal(nft["voting_amount"]) / scale,
        rebase_aero=Decimal(nft["rebase_amount"]) / scale,
        expires_at=nft["expires_at"],
        voted_at=nft["voted_at"],
        permanent=nft["permanent"],
        votes=tuple((lp.lower(), Decimal(weight) / scale) for lp, weight in nft["votes"]),
    )
