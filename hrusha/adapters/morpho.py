"""Morpho adapter: vault positions via the public GraphQL API.

Positions (supplied principal + current value) come from Morpho's free,
keyless GraphQL endpoint — one request per wallet, including emptied
vaults, which matter for historical accounting.

Deposit/withdraw recognition needs no API at all: Morpho vaults are
ERC-4626, so the vault address IS the share token contract. Share
transfers already sit in our ledger (mints on deposit, burns on
withdrawal — the paired asset leg routes through arbitrary zap/router
contracts and is unreliable to match). Each vault therefore seeds two
tag rules keyed on the share contract:
  contract=vault, direction=in  -> deposit  (source morpho)
  contract=vault, direction=out -> withdraw (source morpho)
Both tags are NON_FLOW: moving principal into or out of a yield position
is not income or spend. Gas of those txs inherits the morpho source.

Yield accrues as share-price appreciation, never as transfers — income
events for it are deliberately deferred to the income/spend-semantics
discussion (see memory / design log follow-ups); until then the honest
view is position snapshots vs deposit history.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

import httpx

from hrusha.adapters.known_contracts import SOURCE_MORPHO
from hrusha.ledger.tags import DEPOSIT_TAG, WITHDRAW_TAG, ensure_rule
from hrusha.providers.interface import ProviderError

MORPHO_GRAPHQL_URL = "https://blue-api.morpho.org/graphql"
BASE_CHAIN_ID = 8453
REQUEST_TIMEOUT_SECONDS = 30.0
VAULT_RULE_PRIORITY = 55

POSITIONS_QUERY = """
query ($addr: String!, $chains: [Int!]) {
  vaultPositions(where: { userAddress_in: [$addr], chainId_in: $chains }) {
    items {
      vault { address name symbol asset { symbol decimals } }
      state { shares assets assetsUsd }
    }
  }
}
"""

log = logging.getLogger("hrusha.adapters.morpho")


@dataclass(frozen=True)
class MorphoPosition:
    vault: str  # vault address == ERC-4626 share token contract, lowercase
    vault_name: str
    vault_symbol: str
    asset_symbol: str
    assets: Decimal  # current redeemable value in the underlying asset
    assets_usd: float | None


class MorphoAdapter:
    """Read-only views over Morpho's public GraphQL API (no key)."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)

    def positions(self, address: str) -> list[MorphoPosition]:
        """All vault positions ever held by `address` on Base, current state."""
        try:
            response = self._http.post(
                MORPHO_GRAPHQL_URL,
                json={
                    "query": POSITIONS_QUERY,
                    "variables": {"addr": address, "chains": [BASE_CHAIN_ID]},
                },
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"Morpho API returned HTTP {exc.response.status_code}; re-run later"
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Morpho API request failed ({exc.__class__.__name__}); check connectivity"
            ) from exc
        except ValueError as exc:
            raise ProviderError("Morpho API returned a non-JSON response") from exc
        if body.get("errors"):
            first = body["errors"][0].get("message", "unknown")
            raise ProviderError(f"Morpho API rejected the query: {str(first)[:120]}")
        items = ((body.get("data") or {}).get("vaultPositions") or {}).get("items") or []
        return [_position_from_item(item) for item in items]


def discover_vault_rules(
    conn: sqlite3.Connection, adapter: MorphoAdapter, addresses: list[str]
) -> int:
    """Seed deposit/withdraw rules for every vault the wallets ever used."""
    rules_added = 0
    for address in addresses:
        for position in adapter.positions(address):
            added = ensure_rule(
                conn,
                VAULT_RULE_PRIORITY,
                {"contract": position.vault, "direction": "in"},
                [DEPOSIT_TAG],
                SOURCE_MORPHO,
            )
            added |= ensure_rule(
                conn,
                VAULT_RULE_PRIORITY,
                {"contract": position.vault, "direction": "out"},
                [WITHDRAW_TAG],
                SOURCE_MORPHO,
            )
            if added:
                rules_added += 1
                log.info(
                    "morpho vault rules added",
                    # nb: 'name' is a reserved LogRecord attribute
                    extra={"vault": position.vault, "vault_name": position.vault_name},
                )
    return rules_added


def _position_from_item(item: dict) -> MorphoPosition:
    vault, state = item["vault"], item["state"]
    asset = vault["asset"]
    return MorphoPosition(
        vault=vault["address"].lower(),
        vault_name=vault["name"],
        vault_symbol=vault["symbol"],
        asset_symbol=asset["symbol"],
        assets=Decimal(int(state["assets"])) / Decimal(10) ** int(asset["decimals"]),
        assets_usd=state["assetsUsd"],
    )
