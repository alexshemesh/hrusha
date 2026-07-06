"""Alchemy-backed DataProvider for Base mainnet.

Uses three Alchemy surfaces (one API key):
- JSON-RPC (`base-mainnet.g.alchemy.com`): eth_getBalance for the
  dry-run, alchemy_getAssetTransfers for history, batched
  eth_getTransactionReceipt for exact fees. On Base (OP-stack) the
  total fee is gasUsed * effectiveGasPrice + l1Fee — BaseScan shows
  the same total.
- Portfolio API (`api.g.alchemy.com/data/v1`): token balances with
  USD prices, max 2 wallet addresses per request.
- Prices API (`api.g.alchemy.com/prices/v1`): historical daily USD
  prices; by symbol for ETH, by contract address for tokens.

The API key is embedded in request URLs, so exceptions from the HTTP
layer are never propagated verbatim — httpx error strings include the
URL and would leak the key into logs.

`internal` transfers are not available on Base (Alchemy Transfers API
limitation, Ethereum/Polygon only) — ETH moved by contracts internally
is invisible here until the protocol adapters cover those flows.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import batched

import httpx

from hrusha.providers.interface import ProviderError, TokenBalance, Transfer, TxFee

RPC_URL_TEMPLATE = "https://base-mainnet.g.alchemy.com/v2/{api_key}"
PORTFOLIO_URL_TEMPLATE = "https://api.g.alchemy.com/data/v1/{api_key}/assets/tokens/by-address"
PRICES_URL_TEMPLATE = "https://api.g.alchemy.com/prices/v1/{api_key}/tokens/historical"

NETWORK = "base-mainnet"
CHAIN = "base"
NATIVE_SYMBOL = "ETH"
NATIVE_DECIMALS = 18
WEI_PER_ETH = Decimal(10) ** NATIVE_DECIMALS

REQUEST_TIMEOUT_SECONDS = 30.0
PORTFOLIO_MAX_ADDRESSES = 2
RECEIPT_BATCH_SIZE = 100
TRANSFER_PAGE_LIMIT = 100  # safety cap: 100 pages * 1000 transfers per direction
RETRY_DELAYS_SECONDS: tuple[float, ...] = (1, 2, 4, 8)  # free tier rate-limits routinely

log = logging.getLogger("hrusha.providers.alchemy")

__all__ = ["AlchemyProvider", "ProviderError", "fetch_eth_balances"]


class AlchemyProvider:
    """DataProvider implementation on Alchemy free tier."""

    def __init__(self, api_key: str, http: httpx.Client | None = None) -> None:
        self._api_key = api_key
        self._http = http or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)

    # -- balances -----------------------------------------------------------

    def balances(self, addresses: dict[str, str]) -> list[TokenBalance]:
        url = PORTFOLIO_URL_TEMPLATE.format(api_key=self._api_key)
        balances: list[TokenBalance] = []
        for chunk in batched(addresses.values(), PORTFOLIO_MAX_ADDRESSES):
            payload = {
                "addresses": [{"address": a, "networks": [NETWORK]} for a in chunk],
                "withMetadata": True,
                "withPrices": True,
                "includeNativeTokens": True,
            }
            body = self._post(url, payload, what="Alchemy Portfolio API")
            for raw in body.get("data", {}).get("tokens", []):
                parsed = _token_balance_from_raw(raw)
                if parsed is not None:
                    balances.append(parsed)
        return balances

    # -- transfers ----------------------------------------------------------

    def transfers(self, address: str, since_block: int) -> list[Transfer]:
        found: dict[str, Transfer] = {}  # keyed by uniqueId: a self-transfer shows up twice
        for direction_param in ("fromAddress", "toAddress"):
            for raw in self._transfer_pages(address, since_block, direction_param):
                transfer = _transfer_from_raw(raw, address)
                if transfer is not None:
                    found[raw["uniqueId"]] = transfer
        return sorted(found.values(), key=lambda t: (t.block, t.log_index))

    def _transfer_pages(self, address: str, since_block: int, direction_param: str):
        params: dict = {
            "fromBlock": hex(since_block),
            "toBlock": "latest",
            "category": ["external", "erc20"],
            "withMetadata": True,
            "excludeZeroValue": True,
            "order": "asc",
            direction_param: address,
        }
        for _page in range(TRANSFER_PAGE_LIMIT):
            result = self._rpc("alchemy_getAssetTransfers", [params])
            yield from result.get("transfers", [])
            page_key = result.get("pageKey")
            if not page_key:
                return
            params = {**params, "pageKey": page_key}
        raise ProviderError(
            f"transfer history for one address exceeded {TRANSFER_PAGE_LIMIT} pages; "
            "refusing an unbounded backfill"
        )

    # -- fees ---------------------------------------------------------------

    def tx_fees(self, tx_hashes: list[str], address: str) -> list[TxFee]:
        fees: list[TxFee] = []
        unique_hashes = list(dict.fromkeys(tx_hashes))
        for chunk in batched(unique_hashes, RECEIPT_BATCH_SIZE):
            batch = [
                {
                    "jsonrpc": "2.0",
                    "id": i,
                    "method": "eth_getTransactionReceipt",
                    "params": [tx_hash],
                }
                for i, tx_hash in enumerate(chunk)
            ]
            url = RPC_URL_TEMPLATE.format(api_key=self._api_key)
            responses = self._post(url, batch, what="Alchemy RPC (receipts)")
            if not isinstance(responses, list):
                raise ProviderError("Alchemy RPC batch response is not a list")
            for item in responses:
                receipt = item.get("result")
                if receipt is None:
                    continue  # tx not found / still pending
                if receipt.get("from", "").lower() != address:
                    continue  # incoming tx: someone else paid the fee
                fees.append(
                    TxFee(
                        tx_hash=receipt["transactionHash"],
                        block=int(receipt["blockNumber"], 16),
                        address=address,
                        amount_eth=_total_fee_eth(receipt),
                    )
                )
        return fees

    # -- prices -------------------------------------------------------------

    def historical_usd_price(self, token_key: str, ts: int) -> Decimal | None:
        day_start = datetime.fromtimestamp(ts, tz=UTC).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        payload: dict = {
            "startTime": day_start.isoformat(),
            "endTime": (day_start + timedelta(days=1)).isoformat(),
            "interval": "1d",
        }
        if token_key == NATIVE_SYMBOL:
            payload["symbol"] = NATIVE_SYMBOL
        else:
            payload.update({"network": NETWORK, "address": token_key})
        url = PRICES_URL_TEMPLATE.format(api_key=self._api_key)
        body = self._post(url, payload, what="Alchemy Prices API")
        points = body.get("data") or []
        if not points:
            return None
        return Decimal(str(points[0]["value"]))

    # -- deferred surfaces ----------------------------------------------------

    def positions(self, address: str) -> list:
        raise NotImplementedError("DeFi positions arrive with protocol adapters (Phase 3/4)")

    def claimables(self, address: str) -> list:
        raise NotImplementedError("claimables arrive with protocol adapters (Phase 3/4)")

    # -- plumbing -------------------------------------------------------------

    def _rpc(self, method: str, params: list) -> dict:
        url = RPC_URL_TEMPLATE.format(api_key=self._api_key)
        body = self._post(
            url,
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            what="Alchemy RPC",
        )
        if "error" in body:
            code = body["error"].get("code") if isinstance(body["error"], dict) else None
            raise ProviderError(f"Alchemy RPC error for {method} (code {code})")
        return body.get("result") or {}

    def _post(self, url: str, payload: object, what: str) -> dict | list:
        for delay in (*RETRY_DELAYS_SECONDS, None):  # None marks the final attempt
            try:
                response = self._http.post(url, json=payload)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if delay is not None and (status == 429 or status >= 500):
                    wait = _retry_after_seconds(exc.response) or delay
                    log.warning(
                        "retrying after rate limit / server error",
                        extra={"what": what, "status": status, "wait_seconds": wait},
                    )
                    time.sleep(wait)
                    continue
                hint = (
                    "rate limited even after retries; wait a minute and re-run"
                    if status == 429
                    else "check alchemy.api_key in your config"
                    if status in (401, 403)
                    else "upstream problem; re-run later"
                )
                raise ProviderError(f"{what} returned HTTP {status}; {hint}") from exc
            except httpx.HTTPError as exc:
                raise ProviderError(
                    f"{what} request failed ({exc.__class__.__name__}); check network connectivity"
                ) from exc
            except ValueError as exc:
                raise ProviderError(f"{what} returned a non-JSON response") from exc
        raise AssertionError("unreachable: final attempt either returns or raises")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    try:
        return min(float(raw), 60.0) if raw is not None else None
    except ValueError:
        return None  # HTTP-date form: fall back to our own backoff


# -- raw-response parsing (module-level: pure, fixture-testable) --------------


def _token_balance_from_raw(raw: dict) -> TokenBalance | None:
    contract = raw.get("tokenAddress")
    metadata = raw.get("tokenMetadata") or {}
    symbol = metadata.get("symbol") or (NATIVE_SYMBOL if contract is None else contract)
    decimals = metadata.get("decimals")
    if decimals is None:
        if contract is not None:
            return None  # unknown decimals: unusable amount, almost always spam
        decimals = NATIVE_DECIMALS
    raw_balance = raw.get("tokenBalance")
    if raw_balance is None:
        return None
    amount = Decimal(int(raw_balance, 16)) / Decimal(10) ** int(decimals)
    usd_price = _first_usd_price(raw.get("tokenPrices") or [])
    return TokenBalance(
        address=raw.get("address", "").lower(),
        token=symbol,
        contract=contract.lower() if contract else None,
        amount=amount,
        usd_price=usd_price,
        usd_value=(amount * usd_price) if usd_price is not None else None,
    )


def _first_usd_price(prices: list) -> Decimal | None:
    for entry in prices:
        if entry.get("currency") == "usd" and entry.get("value") is not None:
            return Decimal(str(entry["value"]))
    return None


def _transfer_from_raw(raw: dict, address: str) -> Transfer | None:
    amount = _transfer_amount(raw)
    if amount is None:
        log.warning(
            "skipping transfer with unknown amount",
            extra={"tx_hash": raw.get("hash"), "asset": raw.get("asset")},
        )
        return None
    sender = (raw.get("from") or "").lower()
    contract = (raw.get("rawContract") or {}).get("address")
    timestamp = (raw.get("metadata") or {}).get("blockTimestamp")
    return Transfer(
        tx_hash=raw["hash"],
        log_index=_log_index_from_unique_id(raw.get("uniqueId", "")),
        block=int(raw["blockNum"], 16),
        ts=int(datetime.fromisoformat(timestamp).timestamp()) if timestamp else 0,
        direction="out" if sender == address else "in",
        address=address,
        counterparty=(sender if sender != address else (raw.get("to") or "").lower()) or None,
        token=raw.get("asset") or (contract or NATIVE_SYMBOL),
        contract=contract.lower() if contract else None,
        amount=amount,
    )


def _transfer_amount(raw: dict) -> Decimal | None:
    raw_contract = raw.get("rawContract") or {}
    raw_value, raw_decimals = raw_contract.get("value"), raw_contract.get("decimal")
    if raw_value is not None and raw_decimals is not None:
        return Decimal(int(raw_value, 16)) / Decimal(10) ** int(raw_decimals, 16)
    if raw.get("value") is not None:
        return Decimal(str(raw["value"]))
    return None


def _log_index_from_unique_id(unique_id: str) -> int:
    """'0xhash:log:42' -> 42; '0xhash:external' -> -1 (top-level transfer)."""
    tail = unique_id.rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else -1


def _total_fee_eth(receipt: dict) -> Decimal:
    execution = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
    l1_data_fee = int(receipt.get("l1Fee") or "0x0", 16)
    return Decimal(execution + l1_data_fee) / WEI_PER_ETH


# -- Phase 0 dry-run path ------------------------------------------------------


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
    url = RPC_URL_TEMPLATE.format(api_key=api_key)
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
