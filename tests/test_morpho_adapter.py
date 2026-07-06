"""Morpho adapter: GraphQL parsing, vault rules, sync snapshots."""

from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from hrusha.adapters.morpho import MorphoAdapter, MorphoPosition, discover_vault_rules
from hrusha.config import Config
from hrusha.ledger.ingest import ingest_transfers
from hrusha.ledger.store import open_ledger
from hrusha.ledger.tags import retag_all
from hrusha.prices import PriceResolver
from hrusha.providers.interface import ProviderError
from tests.conftest import COLD, MAIN, FakeProvider, make_transfer

VAULT = "0x" + "e" * 40
ZERO = "0x" + "0" * 40


def graphql_http(items, requests_seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests_seen is not None:
            requests_seen.append(request)
        return httpx.Response(200, json={"data": {"vaultPositions": {"items": items}}})

    return httpx.Client(transport=httpx.MockTransport(handler))


def vault_item(assets="331522807515015961", usd=574.25):
    return {
        "vault": {
            "address": VAULT.upper(),  # API returns checksummed; adapter lowercases
            "name": "Clearstar ETH Reactor",
            "symbol": "CSETH",
            "asset": {"symbol": "WETH", "decimals": 18},
        },
        "state": {"shares": "322603976240903014", "assets": assets, "assetsUsd": usd},
    }


def test_positions_parse_and_scale():
    adapter = MorphoAdapter(http=graphql_http([vault_item()]))
    (position,) = adapter.positions(MAIN)
    assert position == MorphoPosition(
        vault=VAULT,
        vault_name="Clearstar ETH Reactor",
        vault_symbol="CSETH",
        asset_symbol="WETH",
        assets=Decimal("331522807515015961") / Decimal(10) ** 18,
        assets_usd=574.25,
    )


def test_graphql_errors_raise_provider_error():
    def handler(request):
        return httpx.Response(200, json={"errors": [{"message": "boom"}]})

    adapter = MorphoAdapter(http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ProviderError, match="boom"):
        adapter.positions(MAIN)


def test_vault_rules_tag_share_mints_as_deposits(ledger):
    # a deposit as it really looks: shares minted from the zero address,
    # the share token contract being the vault itself
    ingest_transfers(
        ledger,
        [
            make_transfer(counterparty=ZERO, contract=VAULT, token="CSETH"),
            make_transfer(
                counterparty=VAULT, contract=VAULT, token="CSETH", direction="out", log_index=9
            ),
        ],
        tracked_addresses=set(),
        price_fn=lambda token, ts: None,
    )
    adapter = MorphoAdapter(http=graphql_http([vault_item()]))

    assert discover_vault_rules(ledger, adapter, [MAIN]) == 1
    assert discover_vault_rules(ledger, adapter, [MAIN]) == 0  # idempotent
    retag_all(ledger, tracked_addresses=set())

    rows = ledger.execute(
        "SELECT e.kind, e.source, t.tag FROM events e JOIN tags t ON t.event_id = e.id"
    ).fetchall()
    assert ("transfer_in", "morpho", "deposit") in rows
    assert ("transfer_out", "morpho", "withdraw") in rows


def test_sync_snapshots_active_positions_only(tmp_path):
    from hrusha.service.sync import run_full_sync

    config = Config(
        addresses={"main": MAIN, "cold": COLD},
        alchemy_api_key="unused",
        etherscan_api_key=None,
        db_path=Path(tmp_path) / "ledger.db",
    )
    provider = FakeProvider(transfers=[])
    emptied = vault_item(assets="0", usd=0)
    adapter = MorphoAdapter(http=graphql_http([vault_item(), emptied]))
    conn = open_ledger(config.db_path)
    offline = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))

    summary = run_full_sync(
        config, provider, conn, PriceResolver(conn, provider, http=offline), morpho=adapter
    )

    assert summary.morpho_snapshots == 2  # one active vault x two addresses
    rows = conn.execute(
        "SELECT token, source, amount_native, usd_at_time FROM snapshots WHERE kind='position'"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][:2] == ("WETH", "morpho")
    assert rows[0][3] == 574.25
    conn.close()
