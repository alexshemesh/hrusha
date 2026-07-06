"""40acres: seeded vault rules and position snapshots."""

from decimal import Decimal
from pathlib import Path

import httpx

from hrusha.adapters.forty_acres import FortyAcresPosition
from hrusha.adapters.known_contracts import (
    FORTY_ACRES_VAULT,
    USDC_CONTRACT,
    seed_default_rules,
)
from hrusha.config import Config
from hrusha.ledger.ingest import ingest_transfers
from hrusha.ledger.store import open_ledger
from hrusha.ledger.tags import retag_all
from hrusha.prices import PriceResolver
from tests.conftest import COLD, MAIN, FakeProvider, make_transfer


class StubFortyAcres:
    def __init__(self, positions_by_address):
        self._positions = positions_by_address

    def position(self, address):
        return self._positions.get(address)


def test_seed_rules_tag_vault_flows_as_principal_moves(ledger):
    ingest_transfers(
        ledger,
        [
            # USDC out to the vault (deposit) and back (withdrawal)
            make_transfer(direction="out", counterparty=FORTY_ACRES_VAULT, token="USDC"),
            make_transfer(
                direction="in", counterparty=FORTY_ACRES_VAULT, token="USDC", log_index=9
            ),
        ],
        tracked_addresses=set(),
        price_fn=lambda token, ts: None,
    )
    seed_default_rules(ledger)
    retag_all(ledger, tracked_addresses=set())

    rows = ledger.execute(
        "SELECT e.kind, e.source, t.tag FROM events e JOIN tags t ON t.event_id = e.id"
    ).fetchall()
    assert ("transfer_out", "40acres", "deposit") in rows
    assert ("transfer_in", "40acres", "withdraw") in rows


def test_sync_snapshots_forty_acres_position(tmp_path):
    from hrusha.service.sync import run_full_sync

    config = Config(
        addresses={"main": MAIN, "cold": COLD},
        alchemy_api_key="unused",
        etherscan_api_key=None,
        db_path=Path(tmp_path) / "ledger.db",
    )
    adapter = StubFortyAcres(
        {
            MAIN: FortyAcresPosition(
                vault=FORTY_ACRES_VAULT,
                asset_contract=USDC_CONTRACT,
                asset_symbol="USDC",
                assets=Decimal("3945.41"),
            )
        }
    )
    provider = FakeProvider(transfers=[])
    conn = open_ledger(config.db_path)
    offline = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))

    summary = run_full_sync(
        config, provider, conn, PriceResolver(conn, provider, http=offline), forty_acres=adapter
    )

    assert summary.forty_acres_snapshots == 1  # cold wallet has no position
    token, source, amount, usd = conn.execute(
        "SELECT token, source, amount_native, usd_at_time FROM snapshots WHERE source='40acres'"
    ).fetchone()
    assert (token, source, amount) == ("USDC", "40acres", "3945.41")
    assert usd == float(Decimal("3945.41") * 2)  # FakeProvider prices everything at 2.0
    conn.close()
