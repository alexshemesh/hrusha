"""Aerodrome adapter: veNFT parsing, claim-rule discovery, sync snapshots."""

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, call

import httpx
import pytest
from web3 import Web3

from hrusha.adapters.aerodrome import (
    AerodromeAdapter,
    Claimable,
    VeNft,
    VoteHistory,
    _venft_from_tuple,
    discover_claim_rules,
)
from hrusha.adapters.known_contracts import SOURCE_AERODROME
from hrusha.config import Config
from hrusha.ledger.store import open_ledger
from hrusha.ledger.tags import retag_all
from hrusha.prices import PriceResolver
from hrusha.providers.interface import ProviderError
from tests.conftest import COLD, MAIN, OUTSIDER, FakeProvider, make_transfer

REWARD_CONTRACT = "0x" + "c" * 40
ONE_AERO = 10**18


def make_raw_venft(**overrides) -> tuple:
    values = dict(
        id=76592,
        account=MAIN,
        decimals=18,
        amount=5 * ONE_AERO,
        voting_amount=4 * ONE_AERO,
        governance_amount=4 * ONE_AERO,
        rebase_amount=0,
        expires_at=0,
        voted_at=1_750_000_000,
        votes=[(OUTSIDER, 4 * ONE_AERO)],
        token="0x" + "d" * 40,
        permanent=True,
        delegate_id=0,
        managed_id=0,
    )
    values.update(overrides)
    return tuple(values.values())


class StubAdapter:
    """Chain-free stand-in: fixed veNFTs/claimables, scripted verdicts."""

    def __init__(
        self,
        reward_contracts=(),
        venfts_by_address=None,
        claimables_by_id=None,
        vote_history_by_id=None,
    ):
        self._reward_contracts = set(reward_contracts)
        self._venfts = venfts_by_address or {}
        self._claimables = claimables_by_id or {}
        self._vote_history = vote_history_by_id or {}
        self.verdict_calls: list[str] = []
        self.claimable_calls: list[tuple[int, tuple[str, ...]]] = []
        self.vote_history_calls: list[tuple[int, int]] = []

    def is_reward_contract(self, address):
        self.verdict_calls.append(address)
        return address in self._reward_contracts

    def venfts(self, address):
        return self._venfts.get(address, [])

    def claimables(self, venft_id, pools):
        self.claimable_calls.append((venft_id, tuple(pools)))
        return self._claimables.get(venft_id, [])

    def vote_history(self, venft_id, from_block):
        self.vote_history_calls.append((venft_id, from_block))
        return self._vote_history.get(venft_id, VoteHistory((), from_block))


def test_venft_parsing_scales_and_lowercases():
    nft = _venft_from_tuple(make_raw_venft())
    assert nft == VeNft(
        id=76592,
        locked_aero=Decimal(5),
        voting_amount=Decimal(4),
        rebase_aero=Decimal(0),
        expires_at=0,
        voted_at=1_750_000_000,
        permanent=True,
        votes=((OUTSIDER, Decimal(4)),),
    )


def test_claimables_query_each_known_pool_once():
    token = "0x" + "e" * 40
    pool_a = "0x" + "1" * 40
    pool_b = "0x" + "2" * 40
    zero = "0x" + "0" * 40

    adapter = object.__new__(AerodromeAdapter)
    adapter._decimals_cache = {token: 18}
    adapter._rewards_sugar = MagicMock()

    def reward_call(venft_id, pool):
        result = MagicMock()
        result.call.return_value = {
            pool_a: [(venft_id, pool_a, 25 * 10**17, token, REWARD_CONTRACT, zero)],
            pool_b: [(venft_id, pool_b, 5 * 10**17, token, zero, REWARD_CONTRACT)],
        }[pool]
        return result

    adapter._rewards_sugar.functions.rewardsByAddress.side_effect = reward_call

    rewards = adapter.claimables(1, [pool_b, pool_a, pool_a])

    assert adapter._rewards_sugar.functions.rewardsByAddress.call_args_list == [
        call(1, pool_a),
        call(1, pool_b),
    ]
    assert rewards == [
        Claimable(
            venft_id=1,
            pool=pool_a,
            token=token,
            amount=Decimal("2.5"),
            is_fee=True,
        ),
        Claimable(
            venft_id=1,
            pool=pool_b,
            token=token,
            amount=Decimal("0.5"),
            is_fee=False,
        ),
    ]


def test_vote_history_decodes_pool_and_records_head_block():
    pool = "0x" + "1" * 40
    venft_id = 76592
    voted_topic = (
        "0x" + Web3.keccak(text="Voted(address,address,uint256,uint256,uint256,uint256)").hex()
    )
    row = {
        "blockNumber": "0x2a",
        "topics": [
            voted_topic,
            "0x" + "0" * 24 + MAIN[2:],
            "0x" + "0" * 24 + pool[2:],
            "0x" + format(venft_id, "064x"),
        ],
    }

    def handler(request):
        params = request.url.params
        assert params["module"] == "logs"
        assert params["action"] == "getLogs"
        assert params["fromBlock"] == "10"
        assert params["toBlock"] == "50"
        assert params["topic0"] == voted_topic
        assert params["topic3"] == "0x" + format(venft_id, "064x")
        assert params["topic0_3_opr"] == "and"
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": [row]})

    adapter = object.__new__(AerodromeAdapter)
    adapter._w3 = MagicMock()
    adapter._w3.eth.block_number = 50
    adapter._http = httpx.Client(transport=httpx.MockTransport(handler))
    adapter._blockscout_url = "https://example.test/api"

    history = adapter.vote_history(venft_id, from_block=10)

    assert history.pools == (pool,)
    assert history.through_block == 50


def test_vote_history_accepts_normal_empty_result():
    adapter = object.__new__(AerodromeAdapter)
    adapter._w3 = MagicMock()
    adapter._w3.eth.block_number = 50
    adapter._http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"status": "0", "message": "No logs found", "result": []},
            )
        )
    )
    adapter._blockscout_url = "https://example.test/api"

    history = adapter.vote_history(76592, from_block=10)

    assert history.pools == ()
    assert history.through_block == 50


def test_vote_history_rejects_blockscout_result_limit():
    venft_id = 76592
    pool = "0x" + "1" * 40
    voted_topic = (
        "0x" + Web3.keccak(text="Voted(address,address,uint256,uint256,uint256,uint256)").hex()
    )
    row = {
        "blockNumber": "0x2a",
        "topics": [
            voted_topic,
            "0x" + "0" * 24 + MAIN[2:],
            "0x" + "0" * 24 + pool[2:],
            "0x" + format(venft_id, "064x"),
        ],
    }
    adapter = object.__new__(AerodromeAdapter)
    adapter._w3 = MagicMock()
    adapter._w3.eth.block_number = 50
    adapter._http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"status": "1", "message": "OK", "result": [row] * 1_000}
            )
        )
    )
    adapter._blockscout_url = "https://example.test/api"

    with pytest.raises(ProviderError, match="1,000-log limit"):
        adapter.vote_history(venft_id, from_block=0)


def test_vote_history_rejects_non_hex_topics_as_provider_error():
    voted_topic = (
        "0x" + Web3.keccak(text="Voted(address,address,uint256,uint256,uint256,uint256)").hex()
    )
    adapter = object.__new__(AerodromeAdapter)
    adapter._w3 = MagicMock()
    adapter._w3.eth.block_number = 50
    adapter._http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "status": "1",
                    "message": "OK",
                    "result": [
                        {
                            "topics": [
                                voted_topic,
                                "0x" + "0" * 64,
                                "0x" + "0" * 24 + ("1" * 40),
                                "not-hex",
                            ]
                        }
                    ],
                },
            )
        )
    )
    adapter._blockscout_url = "https://example.test/api"

    with pytest.raises(ProviderError, match="malformed Voted log"):
        adapter.vote_history(76592, from_block=0)


def test_discover_claim_rules_creates_rule_and_caches_verdict(ledger):
    from hrusha.ledger.ingest import ingest_transfers

    ingest_transfers(
        ledger,
        [
            make_transfer(counterparty=REWARD_CONTRACT),
            make_transfer(counterparty=OUTSIDER, log_index=9),
        ],
        tracked_addresses=set(),
        price_fn=lambda token, ts: None,
    )
    adapter = StubAdapter(reward_contracts={REWARD_CONTRACT})

    assert discover_claim_rules(ledger, adapter) == 1
    assert sorted(adapter.verdict_calls) == sorted([REWARD_CONTRACT, OUTSIDER])

    # second run: verdicts cached, rule already present -> no chain calls, no dupes
    adapter.verdict_calls.clear()
    assert discover_claim_rules(ledger, adapter) == 0
    assert adapter.verdict_calls == []
    assert ledger.execute("SELECT COUNT(*) FROM tag_rules").fetchone() == (1,)

    retag_all(ledger, tracked_addresses=set())
    tagged = ledger.execute(
        "SELECT e.source, t.tag FROM events e JOIN tags t ON t.event_id = e.id"
        " WHERE e.counterparty = ?",
        (REWARD_CONTRACT,),
    ).fetchall()
    assert (SOURCE_AERODROME, "claim") in tagged


def test_sync_writes_position_and_claimable_snapshots(tmp_path):
    from hrusha.service.sync import run_full_sync

    config = Config(
        addresses={"main": MAIN, "cold": COLD},
        alchemy_api_key="unused",
        etherscan_api_key=None,
        db_path=Path(tmp_path) / "ledger.db",
    )
    provider = FakeProvider(transfers=[])
    current_pool = OUTSIDER
    historical_pool = "0x" + "e" * 40
    stored_pool = "0x" + "f" * 40
    aerodrome = StubAdapter(
        venfts_by_address={
            MAIN: [
                VeNft(
                    id=1,
                    locked_aero=Decimal(100),
                    voting_amount=Decimal(90),
                    rebase_aero=Decimal("2.5"),
                    expires_at=0,
                    voted_at=0,
                    permanent=True,
                    votes=((current_pool, Decimal(90)),),
                )
            ]
        },
        claimables_by_id={
            1: [
                Claimable(
                    venft_id=1,
                    pool=OUTSIDER,
                    token="0x" + "d" * 40,
                    amount=Decimal("12.5"),
                    is_fee=False,
                )
            ]
        },
        vote_history_by_id={1: VoteHistory((historical_pool, current_pool), 123)},
    )
    conn = open_ledger(config.db_path)
    with conn:
        conn.executemany(
            "INSERT INTO sync_state (key, value) VALUES (?, ?)",
            [
                ("aero_vote_pools:1", json.dumps([stored_pool])),
                ("aero_vote_cursor:1", "99"),
            ],
        )
    offline = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))

    summary = run_full_sync(
        config,
        provider,
        conn,
        PriceResolver(conn, provider, http=offline),
        aerodrome=aerodrome,
    )

    assert summary.aerodrome_snapshots == 3
    assert aerodrome.vote_history_calls == [(1, 100)]
    assert aerodrome.claimable_calls == [
        (1, tuple(sorted((current_pool, historical_pool, stored_pool))))
    ]
    state = dict(
        conn.execute(
            "SELECT key, value FROM sync_state WHERE key IN (?, ?)",
            ("aero_vote_pools:1", "aero_vote_cursor:1"),
        ).fetchall()
    )
    assert json.loads(state["aero_vote_pools:1"]) == sorted(
        (current_pool, historical_pool, stored_pool)
    )
    assert state["aero_vote_cursor:1"] == "123"
    rows = conn.execute(
        "SELECT kind, token, source, amount_native FROM snapshots"
        " WHERE kind IN ('position', 'claimable') ORDER BY id"
    ).fetchall()
    assert rows == [
        ("position", "AERO", SOURCE_AERODROME, "100"),
        ("claimable", "AERO", "aerodrome-rebase", "2.5"),
        ("claimable", "0x" + "d" * 40, SOURCE_AERODROME, "12.5"),
    ]
    conn.close()
