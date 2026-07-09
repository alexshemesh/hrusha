"""Well-known public contract addresses on Base, and the tag rules they seed.

Only PUBLIC protocol/token contracts belong here (this path is
gitleaks-allowlisted for raw addresses) — never wallet addresses.
Every address below was verified against live chain data before being
added; Aerodrome reward/voter and Morpho/40acres contracts arrive with
their adapters (Phase 3/4) once verified against real claim txs.
"""

from __future__ import annotations

import sqlite3

# keccak256("Transfer(address,address,uint256)") — the universal ERC-20/721
# Transfer event topic; a public chain constant, not an address (it lives
# here because this path is the repo's one allowlisted home for hex literals)
TRANSFER_EVENT_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# token contracts (Base mainnet)
AERO_CONTRACT = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"  # Aerodrome
USDC_CONTRACT = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # native Circle USDC
WETH_CONTRACT = "0x4200000000000000000000000000000000000006"  # OP-stack predeploy

# Aerodrome Sugar helpers (github.com/velodrome-finance/sugar,
# deployments/base.env); Voter+VeSugar identities confirmed against
# Blockscout verified sources
AERODROME_VOTER = "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"
# FactoryRegistry (sugar deployments/base.env REGISTRY_8453): Sugar's
# pool pagination spans the concatenation of every registered factory's
# pool list, so the registry defines the index space to scan
AERODROME_FACTORY_REGISTRY = "0x5C3F18F06CC09CA1910767A34a20F771039E37C0"
# Aerodrome's legacy Slipstream factory: its pools remain live/votable but the
# current frontend labels them "Migrating" and excludes them from the default
# rewarded-pool views ahead of the MEV-resistant gauge migration.
LEGACY_SLIPSTREAM_POOL_FACTORY = "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a"
LEGACY_SLIPSTREAM_POOL_FACTORIES = frozenset({LEGACY_SLIPSTREAM_POOL_FACTORY})
VE_SUGAR = "0x4d6A741cEE6A8cC5632B2d948C050303F6246D24"
REWARDS_SUGAR = "0x1b121EfDaF4ABb8785a315C51D29BCE0552A7678"
# veAERO escrow (verified as 'VotingEscrow' on Blockscout); AERO sent here
# is locked into veNFTs — a change of form, not spending
VOTING_ESCROW = "0xebf418fe2512e7e6bd9b87a8f0f294acdc67e6b4"

# 40acres supply side (docs.40acres.finance/contracts.md, cross-checked
# on-chain: ERC-4626, asset() == USDC, _loanContract() == the published
# AERO USDC Loan address)
FORTY_ACRES_VAULT = "0xb99b6df96d4d5448cc0a5b3e0ef7896df9507cf5"  # AERO-USDC-Vault

SOURCE_AERODROME = "aerodrome-voting"
SOURCE_AERODROME_REBASE = "aerodrome-rebase"  # anti-dilution AERO, compounds into the lock
SOURCE_MORPHO = "morpho"
SOURCE_40ACRES = "40acres"

# (priority, match, tags, source) — conservative v1 seeds; the discovered
# reward-contract rules (priority 50) outrank the token-based guess
SEED_RULES: tuple[tuple[int, dict, list[str], str | None], ...] = (
    (100, {"contract": AERO_CONTRACT, "direction": "in"}, ["claim", "aero"], SOURCE_AERODROME),
    (60, {"counterparty": VOTING_ESCROW, "direction": "out"}, ["lock"], SOURCE_AERODROME),
    (60, {"counterparty": VOTING_ESCROW, "direction": "in"}, ["unlock"], SOURCE_AERODROME),
    # 40acres AERO-USDC vault: ERC-4626, so both the asset legs
    # (counterparty = vault) and the share legs (contract = vault) are
    # principal movements, not income/spend
    (55, {"counterparty": FORTY_ACRES_VAULT, "direction": "out"}, ["deposit"], SOURCE_40ACRES),
    (55, {"counterparty": FORTY_ACRES_VAULT, "direction": "in"}, ["withdraw"], SOURCE_40ACRES),
    (55, {"contract": FORTY_ACRES_VAULT, "direction": "in"}, ["deposit"], SOURCE_40ACRES),
    (55, {"contract": FORTY_ACRES_VAULT, "direction": "out"}, ["withdraw"], SOURCE_40ACRES),
)


def seed_default_rules(conn: sqlite3.Connection) -> int:
    """Insert any seed rule not already present (by canonical match_json).
    Idempotent and additive: new seeds reach existing databases too."""
    from hrusha.ledger.tags import ensure_rule

    return sum(
        ensure_rule(conn, priority, match, tags, source)
        for priority, match, tags, source in SEED_RULES
    )
