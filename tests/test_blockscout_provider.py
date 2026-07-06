"""BlockscoutProvider against canned Etherscan-style responses.

All chain data is synthetic per the conftest convention (repo is public).
"""

import json
from decimal import Decimal

import httpx
import pytest

import hrusha.providers.blockscout as blockscout
from hrusha.providers.blockscout import PAGE_SIZE, BlockscoutProvider
from hrusha.providers.interface import ProviderError
from tests.conftest import BLOCK_1, MAIN, OUTSIDER, TOKEN_CONTRACT, TS_1, TX_1, TX_2


def native_row(**overrides) -> dict:
    row = {
        "hash": TX_1,
        "blockNumber": str(BLOCK_1),
        "timeStamp": str(TS_1),
        "from": OUTSIDER,
        "to": MAIN,
        "value": "128113929255",  # wei
        "contractAddress": "",
        "isError": "0",
    }
    row.update(overrides)
    return row


def token_row(**overrides) -> dict:
    row = {
        "hash": TX_2,
        "blockNumber": str(BLOCK_1 + 5),
        "timeStamp": str(TS_1 + 10),
        "from": MAIN,
        "to": OUTSIDER,
        "value": "2400000000",
        "contractAddress": TOKEN_CONTRACT,
        "tokenSymbol": "USDC",
        "tokenDecimal": "6",
        "logIndex": None,  # Blockscout really returns null here
    }
    row.update(overrides)
    return row


def ok(rows: list) -> httpx.Response:
    return httpx.Response(200, json={"status": "1", "message": "OK", "result": rows})


def provider_returning(txlist=(), tokentx=()) -> BlockscoutProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        action = request.url.params["action"]
        if int(request.url.params["page"]) > 1:
            return ok([])
        return ok(list(txlist if action == "txlist" else tokentx))

    return BlockscoutProvider(http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_merges_native_and_token_transfers_sorted():
    provider = provider_returning(txlist=[native_row()], tokentx=[token_row()])
    transfers = provider.transfers(MAIN, since_block=0)

    assert [t.token for t in transfers] == ["ETH", "USDC"]
    eth, usdc = transfers
    assert eth.direction == "in"
    assert eth.counterparty == OUTSIDER
    assert eth.log_index == -1
    assert eth.contract is None
    assert eth.ts == TS_1
    assert eth.amount == Decimal("128113929255") / Decimal(10) ** 18
    assert usdc.direction == "out"
    assert usdc.counterparty == OUTSIDER
    assert usdc.contract == TOKEN_CONTRACT
    assert usdc.amount == Decimal("2400")


def test_skips_zero_value_and_failed_native_txs():
    provider = provider_returning(
        txlist=[
            native_row(value="0"),  # approve/vote: a contract call, not a transfer
            native_row(isError="1"),  # reverted: no value moved
        ]
    )
    assert provider.transfers(MAIN, since_block=0) == []


def test_token_transfers_in_one_tx_get_distinct_ordinals():
    provider = provider_returning(tokentx=[token_row(), token_row(value="100")])
    transfers = provider.transfers(MAIN, since_block=0)
    assert [t.log_index for t in transfers] == [0, 1]


def test_spam_token_symbol_is_sanitized():
    hostile = "UЅDС claim ‮now" + "!" * 50  # homoglyphs + RTL override
    provider = provider_returning(tokentx=[token_row(tokenSymbol=hostile)])
    (transfer,) = provider.transfers(MAIN, since_block=0)
    assert "‮" not in transfer.token
    assert len(transfer.token) <= blockscout.MAX_SYMBOL_LENGTH


def test_missing_symbol_falls_back_to_contract():
    provider = provider_returning(tokentx=[token_row(tokenSymbol="")])
    (transfer,) = provider.transfers(MAIN, since_block=0)
    assert transfer.token == TOKEN_CONTRACT


def test_paginates_until_short_page():
    pages_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["action"] == "tokentx":
            return ok([])
        page = int(request.url.params["page"])
        pages_seen.append(page)
        if page == 1:
            return ok(
                [
                    native_row(hash="0x" + "4" * 63 + f"{i % 16:x}", blockNumber=str(BLOCK_1 + i))
                    for i in range(PAGE_SIZE)
                ]
            )
        return ok([native_row(hash=TX_2, blockNumber=str(BLOCK_1 + PAGE_SIZE))])

    provider = BlockscoutProvider(http=httpx.Client(transport=httpx.MockTransport(handler)))
    transfers = provider.transfers(MAIN, since_block=0)
    assert pages_seen == [1, 2]
    assert len(transfers) == PAGE_SIZE + 1


def test_since_block_is_forwarded():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.params["action"]] = request.url.params["startblock"]
        return ok([])

    provider = BlockscoutProvider(http=httpx.Client(transport=httpx.MockTransport(handler)))
    provider.transfers(MAIN, since_block=BLOCK_1)
    assert seen == {"txlist": str(BLOCK_1), "tokentx": str(BLOCK_1)}


def test_no_transactions_found_is_a_normal_empty_result():
    def handler(request: httpx.Request) -> httpx.Response:
        # tokentx phrases its empty result differently from txlist
        noun = "transactions" if request.url.params["action"] == "txlist" else "token transfers"
        return httpx.Response(
            200, json={"status": "0", "message": f"No {noun} found", "result": []}
        )

    provider = BlockscoutProvider(http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert provider.transfers(MAIN, since_block=0) == []


def test_http_429_retries_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(blockscout.time, "sleep", sleeps.append)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, text="slow down")
        if request.url.params["action"] == "txlist":
            return ok([native_row()])
        return ok([])

    provider = BlockscoutProvider(http=httpx.Client(transport=httpx.MockTransport(handler)))
    transfers = provider.transfers(MAIN, since_block=0)
    assert len(transfers) == 1
    assert sleeps == [1]


def test_transport_errors_are_retried(monkeypatch):
    monkeypatch.setattr(blockscout.time, "sleep", lambda _: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ReadTimeout("timed out")
        return ok([])

    provider = BlockscoutProvider(http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert provider.transfers(MAIN, since_block=0) == []
    assert attempts["n"] >= 2


def test_error_status_raises_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "0", "message": "Invalid address format", "result": None}
        )

    provider = BlockscoutProvider(http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError, match="Invalid address format"):
        provider.transfers(MAIN, since_block=0)


def test_non_json_response_raises_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    provider = BlockscoutProvider(http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError, match="non-JSON"):
        provider.transfers(MAIN, since_block=0)


def test_null_log_index_survives_json_roundtrip():
    # guard: token_row's logIndex None must serialize like the real API's null
    assert json.loads(json.dumps(token_row()))["logIndex"] is None
