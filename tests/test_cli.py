from decimal import Decimal

import pytest

import hrusha.cli as cli
from hrusha.config import CONFIG_PATH_ENV_VAR
from hrusha.providers.alchemy_rpc import ProviderError

VALID_CONFIG = (
    "addresses:\n"
    '  main: "0x1111111111111111111111111111111111111111"\n'
    '  cold: "0x2222222222222222222222222222222222222222"\n'
    "alchemy:\n"
    '  api_key: "test-key"\n'
)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(VALID_CONFIG)
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
    assert "0x1111111111111111111111111111111111111111" in out


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


def test_full_sync_not_implemented_yet(config_file, capsys):
    assert cli.main(["sync"]) == cli.EXIT_CONFIG_ERROR
    assert "Phase 1" in capsys.readouterr().err
