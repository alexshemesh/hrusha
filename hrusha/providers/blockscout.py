"""Blockscout-backed transfer source for Base mainnet.

Why not Alchemy or Etherscan for transfer history:
- Alchemy's `alchemy_getAssetTransfers` is load-shed globally for free-tier
  apps whenever their regions degrade (observed as day-long HTTP 429 with
  "unusually high global traffic" while the same key serves plain RPC fine).
- Etherscan v2 dropped Base from its free tier ("Free API access is not
  supported for this chain").

Blockscout's public Base instance (base.blockscout.com) exposes an
Etherscan-compatible account API — no key required, so there is nothing
to leak. `txlist` covers top-level ETH transfers, `tokentx` covers ERC-20.

Quirks handled here:
- `tokentx` returns `logIndex: null`, so token transfers get a synthetic
  per-transaction ordinal (0, 1, ...) as their log_index. It is stable
  because every sync fetches cursor -> head in one pass and rows arrive in
  block order; it is NOT comparable to real log indexes, so mixing this
  source with Alchemy-ingested history in one ledger would duplicate rows.
- Blockscout does not filter spam tokens; symbols are attacker-controlled
  strings (phishing URLs, homoglyphs), so they are stripped of
  non-printable characters and capped. The contract address remains the
  trustworthy token identity.
- Etherscan-style errors arrive as HTTP 200 with status "0"; a plain
  "No transactions found" is a normal empty result, not an error.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from decimal import Decimal

import httpx

from hrusha.providers.interface import ProviderError, Transfer

API_URL = "https://base.blockscout.com/api"

NATIVE_SYMBOL = "ETH"
WEI_PER_ETH = Decimal(10) ** 18
END_BLOCK = 999_999_999
MAX_SYMBOL_LENGTH = 32

PAGE_SIZE = 200
PAGE_LIMIT = 200  # safety cap: 200 pages * 200 rows per action
REQUEST_TIMEOUT_SECONDS = 60.0  # first backfill page over a long history is slow
RETRY_DELAYS_SECONDS: tuple[float, ...] = (1, 2, 4, 8)

log = logging.getLogger("hrusha.providers.blockscout")


class BlockscoutProvider:
    """TransferSource implementation on Blockscout's public Base instance."""

    def __init__(self, http: httpx.Client | None = None, base_url: str = API_URL) -> None:
        self._http = http or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self._base_url = base_url

    def transfers(self, address: str, since_block: int) -> list[Transfer]:
        native = [
            transfer
            for row in self._pages("txlist", address, since_block)
            if (transfer := _native_transfer_from_raw(row, address)) is not None
        ]
        tokens = _token_transfers_from_raw(
            list(self._pages("tokentx", address, since_block)), address
        )
        return sorted(native + tokens, key=lambda t: (t.block, t.log_index))

    # -- plumbing -------------------------------------------------------------

    def _pages(self, action: str, address: str, since_block: int) -> Iterator[dict]:
        for page in range(1, PAGE_LIMIT + 1):
            rows = self._get(
                action,
                {
                    "address": address,
                    "startblock": since_block,
                    "endblock": END_BLOCK,
                    "page": page,
                    "offset": PAGE_SIZE,
                    "sort": "asc",
                },
            )
            yield from rows
            if len(rows) < PAGE_SIZE:
                return
        raise ProviderError(
            f"transfer history for one address exceeded {PAGE_LIMIT} pages; "
            "refusing an unbounded backfill"
        )

    def _get(self, action: str, params: dict) -> list:
        what = f"Blockscout {action}"
        query = {"module": "account", "action": action, **params}
        for delay in (*RETRY_DELAYS_SECONDS, None):  # None marks the final attempt
            try:
                response = self._http.get(self._base_url, params=query)
                response.raise_for_status()
                body = response.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if delay is not None and (status == 429 or status >= 500):
                    log.warning(
                        "retrying after rate limit / server error",
                        extra={"what": what, "status": status, "wait_seconds": delay},
                    )
                    time.sleep(delay)
                    continue
                hint = (
                    "rate limited even after retries; wait a minute and re-run"
                    if status == 429
                    else "upstream problem; re-run later"
                )
                raise ProviderError(f"{what} returned HTTP {status}; {hint}") from exc
            except httpx.HTTPError as exc:
                # timeouts and connection blips: these GETs are idempotent, retry
                if delay is not None:
                    log.warning(
                        "retrying after transport error",
                        extra={
                            "what": what,
                            "error": exc.__class__.__name__,
                            "wait_seconds": delay,
                        },
                    )
                    time.sleep(delay)
                    continue
                raise ProviderError(
                    f"{what} request failed ({exc.__class__.__name__}); check network connectivity"
                ) from exc
            except ValueError as exc:
                raise ProviderError(f"{what} returned a non-JSON response") from exc

            result = body.get("result")
            if body.get("status") == "1":
                return result if isinstance(result, list) else []
            message = str(body.get("message") or "")
            # "No transactions found" / "No token transfers found": empty, not an error
            if message.lower().startswith("no ") and message.lower().endswith(" found"):
                return []
            if delay is not None and "rate limit" in message.lower():
                log.warning(
                    "retrying after in-band rate limit",
                    extra={"what": what, "wait_seconds": delay},
                )
                time.sleep(delay)
                continue
            raise ProviderError(f"{what} returned an error: {message[:120] or 'unknown'}")
        raise AssertionError("unreachable: final attempt either returns or raises")


# -- raw-response parsing (module-level: pure, fixture-testable) --------------


def _native_transfer_from_raw(row: dict, address: str) -> Transfer | None:
    if row.get("isError") == "1":
        return None  # reverted tx: no value moved (its gas cost is a known gap)
    value = int(row.get("value") or 0)
    if value == 0:
        return None  # contract call (approve, vote, ...), not a transfer
    sender = (row.get("from") or "").lower()
    # contract creations have an empty `to` and the new address in contractAddress
    recipient = ((row.get("to") or row.get("contractAddress")) or "").lower()
    return Transfer(
        tx_hash=row["hash"],
        log_index=-1,
        block=int(row["blockNumber"]),
        ts=int(row["timeStamp"]),
        direction="out" if sender == address else "in",
        address=address,
        counterparty=(recipient if sender == address else sender) or None,
        token=NATIVE_SYMBOL,
        contract=None,
        amount=Decimal(value) / WEI_PER_ETH,
    )


def _token_transfers_from_raw(rows: list[dict], address: str) -> list[Transfer]:
    ordinal_by_tx: dict[str, int] = {}
    transfers = []
    for row in rows:
        ordinal = ordinal_by_tx.get(row["hash"], 0)
        ordinal_by_tx[row["hash"]] = ordinal + 1
        transfer = _token_transfer_from_raw(row, address, ordinal)
        if transfer is not None:
            transfers.append(transfer)
    return transfers


def _token_transfer_from_raw(row: dict, address: str, ordinal: int) -> Transfer | None:
    value = int(row.get("value") or 0)
    if value == 0:
        return None
    contract = (row.get("contractAddress") or "").lower()
    sender = (row.get("from") or "").lower()
    recipient = (row.get("to") or "").lower()
    return Transfer(
        tx_hash=row["hash"],
        log_index=ordinal,
        block=int(row["blockNumber"]),
        ts=int(row["timeStamp"]),
        direction="out" if sender == address else "in",
        address=address,
        counterparty=(recipient if sender == address else sender) or None,
        token=_clean_symbol(row.get("tokenSymbol"), fallback=contract),
        contract=contract or None,
        amount=Decimal(value) / Decimal(10) ** int(row.get("tokenDecimal") or 0),
    )


def _clean_symbol(raw: str | None, fallback: str) -> str:
    """Spam token symbols carry phishing text; keep printable chars, cap length."""
    if not raw:
        return fallback
    cleaned = "".join(ch for ch in raw if ch.isprintable()).strip()
    return cleaned[:MAX_SYMBOL_LENGTH] or fallback
