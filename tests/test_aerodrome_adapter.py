"""Aerodrome adapter: veNFT parsing, claim-rule discovery, sync snapshots."""

from decimal import Decimal
from pathlib import Path

import httpx

from hrusha.adapters.aerodrome import (
    Claimable,
    VeNft,
    _venft_from_tuple,
    discover_claim_rules,
)
from hrusha.adapters.known_contracts import SOURCE_AERODROME
from hrusha.config import Config
from hrusha.ledger.store import open_ledger
from hrusha.ledger.tags import retag_all
from hrusha.prices import PriceResolver
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

    def __init__(self, reward_contracts=(), venfts_by_address=None, claimables_by_id=None):
        self._reward_contracts = set(reward_contracts)
        self._venfts = venfts_by_address or {}
        self._claimables = claimables_by_id or {}
        self.verdict_calls: list[str] = []

    def is_reward_contract(self, address):
        self.verdict_calls.append(address)
        return address in self._reward_contracts

    def venfts(self, address):
        return self._venfts.get(address, [])

    def claimables(self, venft_id):
        return self._claimables.get(venft_id, [])


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
                    votes=(),
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
    )
    conn = open_ledger(config.db_path)
    offline = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))

    summary = run_full_sync(
        config,
        provider,
        conn,
        PriceResolver(conn, provider, http=offline),
        aerodrome=aerodrome,
    )

    assert summary.aerodrome_snapshots == 3
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
