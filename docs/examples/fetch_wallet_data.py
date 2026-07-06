"""Fetch everything one sync needs — without writing to the ledger.

Run this before the full program to see what data each provider returns,
whether anything is throttled right now, and how many HTTP requests a
full backfill actually costs:

    .venv/bin/python docs/examples/fetch_wallet_data.py

Reads the normal ~/.hrusha/config.yaml. Read-only: no SQLite, no state.
Output stays on your terminal (addresses/balances are printed locally).
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime

import httpx

from hrusha.config import load_config
from hrusha.providers.alchemy_rpc import AlchemyProvider
from hrusha.providers.blockscout import BlockscoutProvider
from hrusha.providers.interface import ProviderError

request_counts: Counter[str] = Counter()


def counting_client(name: str, timeout: float) -> httpx.Client:
    def count(_request: httpx.Request) -> None:
        request_counts[name] += 1

    return httpx.Client(timeout=timeout, event_hooks={"request": [count]})


def day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def main() -> None:
    config = load_config()
    alchemy = AlchemyProvider(config.alchemy_api_key, http=counting_client("alchemy", 30.0))
    blockscout = BlockscoutProvider(http=counting_client("blockscout", 60.0))
    coingecko = counting_client("coingecko", 30.0)
    started = time.monotonic()

    # 1. current balances (Alchemy Portfolio API)
    print("== balances (Alchemy Portfolio API)")
    try:
        balances = alchemy.balances(config.addresses)
        for b in sorted(balances, key=lambda b: -float(b.usd_value or 0))[:8]:
            usd = f"${float(b.usd_value):,.2f}" if b.usd_value is not None else "?"
            print(f"   {b.token[:16]:<16} {float(b.amount):>18,.6f}  {usd}")
    except ProviderError as exc:
        print(f"   FAILED: {exc}")

    # 2. full transfer history (Blockscout, free, no key)
    print("== transfers (Blockscout)")
    all_transfers = []
    for label, address in config.addresses.items():
        try:
            transfers = blockscout.transfers(address, since_block=0)
        except ProviderError as exc:
            print(f"   {label}: FAILED: {exc}")
            continue
        all_transfers.extend(transfers)
        tokens = {t.token for t in transfers}
        token_days = {(t.contract or t.token, day(t.ts)) for t in transfers}
        print(
            f"   {label}: {len(transfers)} transfers, "
            f"{day(transfers[0].ts)} .. {day(transfers[-1].ts)}, "
            f"{len(tokens)} distinct tokens, "
            f"{len(token_days)} (token, day) pairs to price"
        )

    # 3. exact fees for a few outgoing txs (Alchemy receipts, incl. Base L1 fee)
    print("== fees (Alchemy receipts, sample of 5 outgoing txs)")
    outgoing = [t for t in all_transfers if t.direction == "out"]
    sample = list({t.tx_hash: t for t in outgoing[:5]}.values())
    if sample:
        try:
            fees = alchemy.tx_fees([t.tx_hash for t in sample], sample[0].address)
            for fee in fees:
                print(f"   {fee.tx_hash[:18]}...  {float(fee.amount_eth):.8f} ETH")
        except ProviderError as exc:
            print(f"   FAILED: {exc}")

    # 4. prices: one Alchemy call (throttle check) and one CoinGecko RANGE
    #    call — the range call returns EVERY day in one request, which is
    #    how a backfill should fetch prices (per token, not per token-day)
    print("== prices")
    try:
        price = alchemy.historical_usd_price("ETH", int(time.time()) - 86400)
        print(f"   alchemy ETH yesterday: ${price}")
    except ProviderError as exc:
        print(f"   alchemy prices: FAILED: {exc}")
    if all_transfers:
        first_ts = min(t.ts for t in all_transfers)
        response = coingecko.get(
            "https://api.coingecko.com/api/v3/coins/ethereum/market_chart/range",
            params={"vs_currency": "usd", "from": first_ts, "to": int(time.time())},
        )
        if response.status_code == 200:
            points = response.json().get("prices") or []
            print(
                f"   coingecko ETH range: {len(points)} daily prices "
                f"({day(first_ts)} .. today) in ONE request"
            )
        else:
            print(f"   coingecko range: HTTP {response.status_code}")

    print("== request budget")
    for name, count in sorted(request_counts.items()):
        print(f"   {name:<12} {count} requests")
    print(
        f"   total        {sum(request_counts.values())} requests "
        f"in {time.monotonic() - started:.1f}s"
    )


if __name__ == "__main__":
    main()
