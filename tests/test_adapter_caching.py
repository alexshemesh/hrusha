"""Adapters against the chain-fact cache, and Morpho's once-per-sync fetch.

Adapters are constructed per sync run by design, so anything they read in
their constructor is paid every run unless it is cached.
"""

from decimal import Decimal

from hrusha.adapters.forty_acres import FortyAcresAdapter
from hrusha.adapters.known_contracts import FORTY_ACRES_VAULT
from hrusha.adapters.morpho import MorphoPosition, discover_vault_rules, fetch_positions
from hrusha.ledger.chain_cache import ChainCache

ASSET = "0x" + "d" * 40
WALLET = "0x" + "e" * 40


class CountingW3:
    """web3 stub counting the eth_calls an adapter constructor makes."""

    def __init__(self):
        self.calls = []
        adapter_self = self

        class Eth:
            @staticmethod
            def contract(address=None, abi=None):
                return Contract(address)

        class Contract:
            def __init__(self, address):
                self.address = address
                self.functions = Functions(address)

        class Functions:
            def __init__(self, address):
                self.address = address

            def __getattr__(self, name):
                def call_builder(*args):
                    adapter_self.calls.append(name)
                    values = {
                        "asset": ASSET,
                        "symbol": "USDC",
                        "decimals": 6,
                        "balanceOf": 1_000_000,
                        "convertToAssets": 1_500_000,
                    }
                    return type("Call", (), {"call": staticmethod(lambda: values[name])})()

                return call_builder

        self.eth = Eth()


class FakeWeb3Address:
    @staticmethod
    def to_checksum_address(address):
        return address


def make_adapter(w3, cache=None, monkeypatch=None):
    """FortyAcresAdapter with web3's checksum helper stubbed out."""
    import hrusha.adapters.forty_acres as module

    monkeypatch.setattr(module.Web3, "to_checksum_address", staticmethod(lambda a: a))
    return FortyAcresAdapter(w3, FORTY_ACRES_VAULT, cache)


def test_cold_cache_reads_the_vault_then_stores_it(ledger, monkeypatch):
    cache = ChainCache(ledger)
    w3 = CountingW3()
    make_adapter(w3, cache, monkeypatch)

    assert w3.calls == ["asset", "symbol", "decimals"]
    assert cache.erc4626_asset(FORTY_ACRES_VAULT) == ASSET
    assert cache.token_meta(ASSET) == ("USDC", 6)


def test_warm_cache_constructs_without_any_chain_read(ledger, monkeypatch):
    """The vault's asset and that asset's symbol/decimals never change."""
    cache = ChainCache(ledger)
    make_adapter(CountingW3(), cache, monkeypatch)  # warm it

    second = CountingW3()
    adapter = make_adapter(second, cache, monkeypatch)

    assert second.calls == []
    position = adapter.position(WALLET)
    assert position.asset_symbol == "USDC"
    assert position.assets == Decimal("1.5")  # 1_500_000 / 10**6
    assert second.calls == ["balanceOf", "convertToAssets"]  # the live part still runs


def test_adapter_still_works_without_a_cache(monkeypatch):
    """Tests and probes construct adapters with no DB in sight."""
    w3 = CountingW3()
    adapter = make_adapter(w3, None, monkeypatch)
    assert w3.calls == ["asset", "symbol", "decimals"]
    assert adapter.position(WALLET).asset_symbol == "USDC"


# -- Morpho: one GraphQL call per wallet per sync -----------------------------


class CountingMorpho:
    def __init__(self):
        self.positions_calls = 0

    def positions(self, address):
        self.positions_calls += 1
        return [
            MorphoPosition(
                vault="0x" + "f" * 40,
                vault_name="Vault",
                vault_symbol="mV",
                asset_symbol="USDC",
                assets=Decimal("10"),
                assets_usd=10.0,
            )
        ]


def test_rule_discovery_reuses_positions_the_caller_already_fetched(ledger):
    adapter = CountingMorpho()
    positions = fetch_positions(adapter, [WALLET])
    assert adapter.positions_calls == 1

    assert discover_vault_rules(ledger, adapter, [WALLET], positions) == 1
    assert adapter.positions_calls == 1  # no second fetch for rule discovery


def test_rule_discovery_still_fetches_when_given_nothing(ledger):
    adapter = CountingMorpho()
    assert discover_vault_rules(ledger, adapter, [WALLET]) == 1
    assert adapter.positions_calls == 1
