from decimal import Decimal

import httpx
import pytest

import hrusha.cli as cli
from hrusha.config import CONFIG_PATH_ENV_VAR
from hrusha.prices import PriceResolver
from hrusha.providers.alchemy_rpc import ProviderError
from tests.conftest import COLD, MAIN, TX_OWN, FakeProvider, make_fee, make_transfer


def offline_resolver(conn, provider) -> PriceResolver:
    """PriceResolver whose DefiLlama calls fail: no network from unit tests."""
    offline = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    return PriceResolver(conn, provider, http=offline)


VALID_CONFIG_TEMPLATE = (
    "addresses:\n"
    f'  main: "{MAIN}"\n'
    f'  cold: "{COLD}"\n'
    "alchemy:\n"
    '  api_key: "test-key"\n'
    'db_path: "{db_path}"\n'
)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIG_TEMPLATE.format(db_path=tmp_path / "ledger.db"))
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(path))
    return path


def test_dry_run_prints_balances(config_file, monkeypatch, capsys):
    def fake_fetch(api_key, addresses):
        assert api_key == "test-key"
        return {label: Decimal("1.5") for label in addresses}

    monkeypatch.setattr(cli, "fetch_eth_balances", fake_fetch)
    assert cli.main(["sync", "--dry-run"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "main" in out and "cold" in out
    assert "1.500000 ETH" in out
    assert MAIN in out


def test_missing_config_exits_with_config_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(CONFIG_PATH_ENV_VAR, str(tmp_path / "missing.yaml"))
    assert cli.main(["sync", "--dry-run"]) == cli.EXIT_CONFIG_ERROR
    assert "config file not found" in capsys.readouterr().err


def test_provider_failure_exits_with_provider_error(config_file, monkeypatch, capsys):
    def failing_fetch(api_key, addresses):
        raise ProviderError("Alchemy RPC returned HTTP 401; check alchemy.api_key")

    monkeypatch.setattr(cli, "fetch_eth_balances", failing_fetch)
    assert cli.main(["sync", "--dry-run"]) == cli.EXIT_PROVIDER_ERROR
    assert "HTTP 401" in capsys.readouterr().err


def test_full_sync_then_reports(config_file, monkeypatch, capsys):
    provider = FakeProvider(
        transfers=[make_transfer(direction="out"), make_transfer(log_index=8)],
        fees=[make_fee()],
    )
    monkeypatch.setattr(cli, "AlchemyProvider", lambda api_key: provider)
    monkeypatch.setattr(cli, "BlockscoutProvider", lambda: provider)
    monkeypatch.setattr(cli, "PriceResolver", offline_resolver)
    monkeypatch.setattr(cli, "make_aerodrome_adapter", lambda config: None)
    monkeypatch.setattr(cli, "MorphoAdapter", lambda: None)

    assert cli.main(["sync"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "2 transfers ingested" in out
    assert "1 fee events" in out

    # second sync: cursor advanced past the last block, nothing refetched
    assert cli.main(["sync"]) == cli.EXIT_OK
    assert "0 transfers ingested" in capsys.readouterr().out

    assert cli.main(["transfers"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "USDC" in out and "main" in out

    assert cli.main(["fees", "--days", "36500"]) == cli.EXIT_OK
    assert "1 txs" in capsys.readouterr().out


def test_report_tag_and_retag_commands(config_file, monkeypatch, capsys):
    provider = FakeProvider(
        # distinct txs: an in+out pair in ONE tx would (correctly) be a swap
        transfers=[
            make_transfer(direction="in", tx_hash=TX_OWN),
            make_transfer(log_index=8, direction="out"),
        ],
        fees=[make_fee()],
    )
    monkeypatch.setattr(cli, "AlchemyProvider", lambda api_key: provider)
    monkeypatch.setattr(cli, "BlockscoutProvider", lambda: provider)
    monkeypatch.setattr(cli, "PriceResolver", offline_resolver)
    monkeypatch.setattr(cli, "make_aerodrome_adapter", lambda config: None)
    monkeypatch.setattr(cli, "MorphoAdapter", lambda: None)
    assert cli.main(["sync"]) == cli.EXIT_OK
    capsys.readouterr()

    assert cli.main(["report", "--days", "36500"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "untagged" in out and "neto = income - gas" in out
    assert "2025-06-12" in out  # TS_1's epoch flip date (Thursday, UTC)

    assert cli.main(["report", "--days", "36500", "--coins"]) == cli.EXIT_OK
    assert "USDC" in capsys.readouterr().out

    assert cli.main(["tag", "1", "bribe"]) == cli.EXIT_OK
    assert "manual" in capsys.readouterr().out
    assert cli.main(["tag", "9999", "bribe"]) == cli.EXIT_NOT_FOUND
    capsys.readouterr()

    assert cli.main(["retag"]) == cli.EXIT_OK
    assert "rules run" in capsys.readouterr().out

    assert cli.main(["transfers"]) == cli.EXIT_OK
    assert "bribe" in capsys.readouterr().out  # manual tag survived the retag


def test_balances_command(config_file, monkeypatch, capsys):
    monkeypatch.setattr(cli, "AlchemyProvider", lambda api_key: FakeProvider())
    assert cli.main(["balances"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "ETH" in out
    assert "4,500.00" in out
