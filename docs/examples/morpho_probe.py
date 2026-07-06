"""Phase 4 spike: Morpho vault positions via the public GraphQL API.

Read-only probe against the real wallet — no SQLite, no sync, no state.
Shows every Morpho vault position on Base (including emptied ones, which
matter for historical yield accounting), the current asset value, and
the share balance.

No API key needed. Run:  .venv/bin/python docs/examples/morpho_probe.py

Notes for the adapter (hrusha/adapters/morpho.py):
- positions live under VaultPosition.state (shares/assets/assetsUsd)
- vault share tokens (CSETH, exmWETH, ...) are plain ERC-20s, so vault
  deposits/withdrawals already sit in our transfer history as
  asset-out + shares-in pairs — the swap detector currently classifies
  them, which the adapter must refine into deposit/withdraw
- yield accrues as share-price appreciation, NOT transfers: income =
  assets now minus net deposited; needs position snapshots over time
"""

from __future__ import annotations

import json

import httpx

from hrusha.config import load_config

MORPHO_GRAPHQL_URL = "https://blue-api.morpho.org/graphql"
BASE_CHAIN_ID = 8453

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


def main() -> None:
    config = load_config()
    for label, address in config.addresses.items():
        print(f"== {label} ({address})")
        response = httpx.post(
            MORPHO_GRAPHQL_URL,
            json={
                "query": POSITIONS_QUERY,
                "variables": {"addr": address, "chains": [BASE_CHAIN_ID]},
            },
            timeout=30,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("errors"):
            print("   GraphQL errors:", json.dumps(body["errors"])[:300])
            continue
        items = body["data"]["vaultPositions"]["items"]
        if not items:
            print("   no Morpho positions")
            continue
        for item in items:
            vault, state = item["vault"], item["state"]
            asset = vault["asset"]
            assets = int(state["assets"]) / 10 ** asset["decimals"]
            flag = "ACTIVE" if int(state["assets"]) else "emptied"
            print(
                f"   [{flag:<7}] {vault['name']:<28} ({vault['symbol']}): "
                f"{assets:,.6f} {asset['symbol']} = ${state['assetsUsd'] or 0:,.2f}"
            )


if __name__ == "__main__":
    main()
