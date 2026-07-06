"""Well-known public contract addresses on Base, and the tag rules they seed.

Only PUBLIC protocol/token contracts belong here (this path is
gitleaks-allowlisted for raw addresses) — never wallet addresses.
Every address below was verified against live chain data before being
added; Aerodrome reward/voter and Morpho/40acres contracts arrive with
their adapters (Phase 3/4) once verified against real claim txs.
"""

from __future__ import annotations

import sqlite3

# token contracts (Base mainnet)
AERO_CONTRACT = "0x940181a94a35a4569e4529a3cdfb74e38fd98631"  # Aerodrome
USDC_CONTRACT = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # native Circle USDC
WETH_CONTRACT = "0x4200000000000000000000000000000000000006"  # OP-stack predeploy

# Aerodrome Sugar helpers (github.com/velodrome-finance/sugar,
# deployments/base.env); Voter+VeSugar identities confirmed against
# Blockscout verified sources
AERODROME_VOTER = "0x16613524e02ad97eDfeF371bC883F2F5d6C480A5"
VE_SUGAR = "0x4d6A741cEE6A8cC5632B2d948C050303F6246D24"
REWARDS_SUGAR = "0x1b121EfDaF4ABb8785a315C51D29BCE0552A7678"
# veAERO escrow (verified as 'VotingEscrow' on Blockscout); AERO sent here
# is locked into veNFTs — a change of form, not spending
VOTING_ESCROW = "0xebf418fe2512e7e6bd9b87a8f0f294acdc67e6b4"

SOURCE_AERODROME = "aerodrome-voting"
SOURCE_MORPHO = "morpho"

# (priority, match, tags, source) — conservative v1 seeds; the discovered
# reward-contract rules (priority 50) outrank the token-based guess
SEED_RULES: tuple[tuple[int, dict, list[str], str | None], ...] = (
    (100, {"contract": AERO_CONTRACT, "direction": "in"}, ["claim", "aero"], SOURCE_AERODROME),
    (60, {"counterparty": VOTING_ESCROW, "direction": "out"}, ["lock"], SOURCE_AERODROME),
    (60, {"counterparty": VOTING_ESCROW, "direction": "in"}, ["unlock"], SOURCE_AERODROME),
)


def seed_default_rules(conn: sqlite3.Connection) -> int:
    """Insert any seed rule not already present (by canonical match_json).
    Idempotent and additive: new seeds reach existing databases too."""
    from hrusha.ledger.tags import ensure_rule

    return sum(
        ensure_rule(conn, priority, match, tags, source)
        for priority, match, tags, source in SEED_RULES
    )
