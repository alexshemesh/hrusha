"""40acres adapter: supply-side position in the AERO-USDC vault.

40acres (docs.40acres.finance) issues self-repaying loans against
veNFTs; we are on the SUPPLY side only — USDC lent into the published
AERO-USDC vault. The vault is plain ERC-4626, so:

- the position is two eth_calls: balanceOf(wallet) -> shares,
  convertToAssets(shares) -> redeemable USDC;
- deposits/withdrawals need no adapter at all — the vault address is
  static and published, so known_contracts.SEED_RULES tags both the
  asset legs (counterparty = vault) and share legs (contract = vault)
  as deposit/withdraw, source 40acres.

Supply yield accrues as share-price appreciation (the vault's
epochRewardsLocked drips epoch rewards into totalAssets) — income
events are deferred to the income/spend-semantics discussion, same as
Morpho; until then position snapshots vs deposit history carry it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from web3 import Web3

from hrusha.adapters.known_contracts import FORTY_ACRES_VAULT

log = logging.getLogger("hrusha.adapters.forty_acres")

ERC4626_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "convertToAssets",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "shares", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "asset",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]

ERC20_META_ABI = [
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
]


@dataclass(frozen=True)
class FortyAcresPosition:
    vault: str
    asset_contract: str  # lowercase; the pricing identity
    asset_symbol: str
    assets: Decimal  # redeemable value in the underlying asset


class FortyAcresAdapter:
    """Read-only ERC-4626 views over the 40acres supply vault."""

    def __init__(self, w3: Web3, vault: str = FORTY_ACRES_VAULT) -> None:
        self._vault_address = vault.lower()
        self._vault = w3.eth.contract(address=Web3.to_checksum_address(vault), abi=ERC4626_ABI)
        asset_address = self._vault.functions.asset().call()
        asset = w3.eth.contract(address=asset_address, abi=ERC20_META_ABI)
        self._asset_contract = asset_address.lower()
        self._asset_symbol = asset.functions.symbol().call()
        self._asset_decimals = asset.functions.decimals().call()

    def position(self, address: str) -> FortyAcresPosition | None:
        """Current redeemable position of `address`, or None when empty."""
        shares = self._vault.functions.balanceOf(Web3.to_checksum_address(address)).call()
        if shares == 0:
            return None
        assets = self._vault.functions.convertToAssets(shares).call()
        return FortyAcresPosition(
            vault=self._vault_address,
            asset_contract=self._asset_contract,
            asset_symbol=self._asset_symbol,
            assets=Decimal(assets) / Decimal(10) ** self._asset_decimals,
        )
