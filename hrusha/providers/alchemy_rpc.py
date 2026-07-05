"""Minimal Alchemy JSON-RPC client for Base mainnet.

Phase 0 covers only native ETH balances (the `sync --dry-run` smoke
path). Phase 1 grows this into the full DataProvider implementation
(Portfolio API, getAssetTransfers, receipts).

The API key is embedded in the request URL, so exceptions from the
HTTP layer are never propagated verbatim — httpx error strings include
the URL and would leak the key into logs.
"""

from __future__ import annotations

from decimal import Decimal

import httpx

BASE_RPC_URL_TEMPLATE = "https://base-mainnet.g.alchemy.com/v2/{api_key}"
REQUEST_TIMEOUT_SECONDS = 15.0
WEI_PER_ETH = Decimal(10) ** 18


class ProviderError(Exception):
    """An upstream data-provider call failed. Messages never contain the API key."""


def fetch_eth_balances(api_key: str, addresses: dict[str, str]) -> dict[str, Decimal]:
    """Return the native ETH balance per address label, in ETH."""
    labels = list(addresses)
    batch = [
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "eth_getBalance",
            "params": [addresses[label], "latest"],
        }
        for request_id, label in enumerate(labels)
    ]
    url = BASE_RPC_URL_TEMPLATE.format(api_key=api_key)
    try:
        response = httpx.post(url, json=batch, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        results = response.json()
    except httpx.HTTPStatusError as exc:
        raise ProviderError(
            f"Alchemy RPC returned HTTP {exc.response.status_code}; "
            "check alchemy.api_key in your config"
        ) from exc
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"Alchemy RPC request failed ({exc.__class__.__name__}); check network connectivity"
        ) from exc
    except ValueError as exc:
        raise ProviderError("Alchemy RPC returned a non-JSON response") from exc

    return _balances_from_batch_response(results, labels)


def _balances_from_batch_response(results: object, labels: list[str]) -> dict[str, Decimal]:
    if not isinstance(results, list):
        raise ProviderError("Alchemy RPC batch response is not a list")
    balances: dict[str, Decimal] = {}
    for item in results:
        request_id = item.get("id")
        if not isinstance(request_id, int) or not 0 <= request_id < len(labels):
            raise ProviderError("Alchemy RPC response contains an unknown request id")
        label = labels[request_id]
        if "error" in item:
            code = item["error"].get("code") if isinstance(item["error"], dict) else None
            raise ProviderError(f"Alchemy RPC error for address {label!r} (code {code})")
        balances[label] = Decimal(int(item["result"], 16)) / WEI_PER_ETH
    if len(balances) != len(labels):
        missing = sorted(set(labels) - set(balances))
        raise ProviderError(f"Alchemy RPC response is missing results for: {', '.join(missing)}")
    return balances
