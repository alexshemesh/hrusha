"""AlchemyProvider against recorded-shape synthetic responses (httpx.MockTransport)."""

import json
from decimal import Decimal

import httpx
import pytest

from hrusha.providers.alchemy_rpc import AlchemyProvider, ProviderError
from tests.conftest import COLD, MAIN, OUTSIDER, TOKEN_CONTRACT, TX_1, TX_2

WEI_15 = hex(1_500_000_000_000_000_000)  # 1.5 ETH


def make_provider(handler):
    transport = httpx.MockTransport(handler)
    return AlchemyProvider("test-key", http=httpx.Client(transport=transport))


# -- balances -----------------------------------------------------------------


def portfolio_response():
    return {
        "data": {
            "tokens": [
                {
                    "address": MAIN,
                    "network": "base-mainnet",
                    "tokenAddress": None,
                    "tokenBalance": WEI_15,
                    "tokenMetadata": None,
                    "tokenPrices": [{"currency": "usd", "value": "3000"}],
                },
                {
                    "address": MAIN,
                    "network": "base-mainnet",
                    "tokenAddress": TOKEN_CONTRACT,
                    "tokenBalance": hex(2_500_000),
                    "tokenMetadata": {"symbol": "USDC", "decimals": 6},
                    "tokenPrices": [{"currency": "usd", "value": "1.0"}],
                },
                {
                    "address": MAIN,
                    "network": "base-mainnet",
                    "tokenAddress": OUTSIDER,
                    "tokenBalance": "0x1",
                    "tokenMetadata": {"symbol": "SPAM", "decimals": None},
                    "tokenPrices": [],
                },
            ]
        }
    }


def test_balances_parses_native_and_erc20():
    def handler(request):
        assert "/data/v1/" in request.url.path
        return httpx.Response(200, json=portfolio_response())

    balances = make_provider(handler).balances({"main": MAIN})
    assert len(balances) == 2  # spam token with unknown decimals dropped
    native = next(b for b in balances if b.contract is None)
    assert native.token == "ETH"
    assert native.amount == Decimal("1.5")
    assert native.usd_value == Decimal("4500")
    usdc = next(b for b in balances if b.contract == TOKEN_CONTRACT)
    assert usdc.amount == Decimal("2.5")
    assert usdc.usd_price == Decimal("1.0")


def test_balances_chunks_requests_by_two_addresses():
    request_counts = []

    def handler(request):
        body = json.loads(request.content)
        request_counts.append(len(body["addresses"]))
        return httpx.Response(200, json={"data": {"tokens": []}})

    make_provider(handler).balances({"a": MAIN, "b": COLD, "c": OUTSIDER})
    assert request_counts == [2, 1]


def test_http_error_never_leaks_key():
    def handler(request):
        return httpx.Response(401, json={})

    with pytest.raises(ProviderError) as excinfo:
        make_provider(handler).balances({"main": MAIN})
    assert "test-key" not in str(excinfo.value)
    assert "401" in str(excinfo.value)


# -- transfers ------------------------------------------------------------------


def raw_transfer(tx_hash, unique_suffix, from_addr, to_addr, block=31_000_000):
    return {
        "blockNum": hex(block),
        "uniqueId": f"{tx_hash}:{unique_suffix}",
        "hash": tx_hash,
        "from": from_addr,
        "to": to_addr,
        "value": 100.5,
        "asset": "USDC",
        "category": "erc20",
        "rawContract": {
            "value": hex(100_500_000),
            "address": TOKEN_CONTRACT,
            "decimal": "0x6",
        },
        "metadata": {"blockTimestamp": "2026-06-15T12:00:00.000Z"},
    }


def transfers_handler(pages_by_direction):
    """pages_by_direction: {'fromAddress': [page1, page2], 'toAddress': [page1]}"""

    def handler(request):
        body = json.loads(request.content)
        if isinstance(body, dict) and body.get("method") == "alchemy_getAssetTransfers":
            params = body["params"][0]
            direction = "fromAddress" if "fromAddress" in params else "toAddress"
            pages = pages_by_direction[direction]
            index = 0 if "pageKey" not in params else int(params["pageKey"])
            result = {"transfers": pages[index]}
            if index + 1 < len(pages):
                result["pageKey"] = str(index + 1)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})
        raise AssertionError("unexpected request")

    return handler


def test_transfers_paginate_and_merge_directions():
    outgoing = raw_transfer(TX_1, "log:7", MAIN, OUTSIDER, block=100)
    outgoing_page2 = raw_transfer(TX_2, "log:9", MAIN, OUTSIDER, block=300)
    incoming = raw_transfer(TX_2, "log:8", OUTSIDER, MAIN, block=200)
    handler = transfers_handler(
        {"fromAddress": [[outgoing], [outgoing_page2]], "toAddress": [[incoming]]}
    )

    transfers = make_provider(handler).transfers(MAIN, since_block=0)
    assert [t.block for t in transfers] == [100, 200, 300]  # oldest first
    assert transfers[0].direction == "out"
    assert transfers[0].counterparty == OUTSIDER
    assert transfers[0].log_index == 7
    assert transfers[0].amount == Decimal("100.5")
    assert transfers[0].ts > 0
    assert transfers[1].direction == "in"


def test_self_transfer_not_duplicated():
    self_transfer = raw_transfer(TX_1, "log:7", MAIN, MAIN)
    handler = transfers_handler({"fromAddress": [[self_transfer]], "toAddress": [[self_transfer]]})
    transfers = make_provider(handler).transfers(MAIN, since_block=0)
    assert len(transfers) == 1
    assert transfers[0].direction == "out"


def test_external_transfer_has_no_log_index():
    external = raw_transfer(TX_1, "external", OUTSIDER, MAIN)
    external["asset"] = "ETH"
    external["category"] = "external"
    external["rawContract"] = {"value": WEI_15, "address": None, "decimal": "0x12"}
    handler = transfers_handler({"fromAddress": [[]], "toAddress": [[external]]})
    transfers = make_provider(handler).transfers(MAIN, since_block=0)
    assert transfers[0].log_index == -1
    assert transfers[0].token == "ETH"
    assert transfers[0].contract is None
    assert transfers[0].amount == Decimal("1.5")


# -- fees -----------------------------------------------------------------------


def receipt(tx_hash, sender, gas_used=21000, gas_price=10**8, l1_fee=5 * 10**11):
    return {
        "transactionHash": tx_hash,
        "blockNumber": hex(31_000_000),
        "from": sender,
        "gasUsed": hex(gas_used),
        "effectiveGasPrice": hex(gas_price),
        "l1Fee": hex(l1_fee),
    }


def test_tx_fees_include_l1_fee_and_skip_foreign_senders():
    def handler(request):
        body = json.loads(request.content)
        assert isinstance(body, list)
        results = {TX_1: receipt(TX_1, MAIN), TX_2: receipt(TX_2, OUTSIDER)}
        return httpx.Response(
            200,
            json=[
                {"jsonrpc": "2.0", "id": item["id"], "result": results[item["params"][0]]}
                for item in body
            ],
        )

    fees = make_provider(handler).tx_fees([TX_1, TX_2, TX_1], MAIN)  # TX_1 duplicated
    assert len(fees) == 1  # foreign sender skipped, duplicate collapsed
    expected = Decimal(21000 * 10**8 + 5 * 10**11) / Decimal(10**18)
    assert fees[0].amount_eth == expected


# -- historical prices ------------------------------------------------------------


def test_historical_price_by_symbol_for_eth():
    seen_bodies = []

    def handler(request):
        seen_bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json={"data": [{"value": "3000.5", "timestamp": "2026-06-15T00:00:00Z"}]}
        )

    price = make_provider(handler).historical_usd_price("ETH", 1_750_000_000)
    assert price == Decimal("3000.5")
    assert seen_bodies[0]["symbol"] == "ETH"
    assert "network" not in seen_bodies[0]


def test_historical_price_missing_returns_none():
    def handler(request):
        return httpx.Response(200, json={"data": []})

    assert make_provider(handler).historical_usd_price(TOKEN_CONTRACT, 1_750_000_000) is None


# -- retries ----------------------------------------------------------------------


def test_429_retried_then_succeeds(monkeypatch):
    import hrusha.providers.alchemy_rpc as mod

    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, json={}, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"data": {"tokens": []}})

    assert make_provider(handler).balances({"main": MAIN}) == []
    assert len(attempts) == 3
    assert sleeps == [2.0, 2.0]  # Retry-After honored


def test_429_exhausted_reports_rate_limit_not_key(monkeypatch):
    import hrusha.providers.alchemy_rpc as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: None)

    def handler(request):
        return httpx.Response(429, json={})

    with pytest.raises(ProviderError) as excinfo:
        make_provider(handler).balances({"main": MAIN})
    assert "rate limited" in str(excinfo.value)
    assert "api_key" not in str(excinfo.value)
