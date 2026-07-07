import textwrap

import pytest

from hrusha.config import DEFAULT_DB_PATH, ConfigError, load_config

VALID_CONFIG = textwrap.dedent(
    """
    addresses:
      main: "0x1111111111111111111111111111111111111111"
      cold: "0x2222222222222222222222222222222222222222"
    alchemy:
      api_key: "test-alchemy-key"
    etherscan:
      api_key: "test-etherscan-key"
    """
)


def write_config(tmp_path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    return path


def test_valid_config_loads(tmp_path):
    config = load_config(write_config(tmp_path, VALID_CONFIG))
    assert config.addresses == {
        "main": "0x1111111111111111111111111111111111111111",
        "cold": "0x2222222222222222222222222222222222222222",
    }
    assert config.alchemy_api_key == "test-alchemy-key"
    assert config.etherscan_api_key == "test-etherscan-key"
    assert config.db_path == DEFAULT_DB_PATH.expanduser()


def test_addresses_are_lowercased(tmp_path):
    content = VALID_CONFIG.replace(
        "0x1111111111111111111111111111111111111111",
        "0x" + "A" * 40,
    )
    config = load_config(write_config(tmp_path, content))
    assert config.addresses["main"] == "0x" + "a" * 40


def test_missing_file_names_path(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError, match=str(missing)):
        load_config(missing)


def test_missing_addresses_key(tmp_path):
    path = write_config(tmp_path, "alchemy:\n  api_key: k\n")
    with pytest.raises(ConfigError, match="addresses"):
        load_config(path)


def test_invalid_address_names_label_not_value(tmp_path):
    secret_value = "0xZZ11111111111111111111111111111111111111"
    path = write_config(
        tmp_path,
        f'addresses:\n  hot: "{secret_value}"\nalchemy:\n  api_key: k\n',
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "addresses.hot" in str(excinfo.value)
    assert secret_value not in str(excinfo.value)


def test_missing_alchemy_key(tmp_path):
    path = write_config(
        tmp_path, 'addresses:\n  main: "0x1111111111111111111111111111111111111111"\n'
    )
    with pytest.raises(ConfigError, match=r"alchemy\.api_key"):
        load_config(path)


def test_etherscan_is_optional(tmp_path):
    path = write_config(
        tmp_path,
        'addresses:\n  main: "0x1111111111111111111111111111111111111111"\n'
        "alchemy:\n  api_key: k\n",
    )
    assert load_config(path).etherscan_api_key is None


def test_invalid_yaml_does_not_echo_content(tmp_path):
    secret_line = 'api_key: "super-secret-value'  # unterminated quote -> YAML error
    path = write_config(tmp_path, f"alchemy:\n  {secret_line}\n")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "super-secret-value" not in str(excinfo.value)


def test_custom_db_path(tmp_path):
    path = write_config(tmp_path, VALID_CONFIG + 'db_path: "/data/hrusha.db"\n')
    assert str(load_config(path).db_path) == "/data/hrusha.db"


def test_vote_scout_defaults_when_section_absent(tmp_path):
    from hrusha.config import ScoutFilters

    config = load_config(write_config(tmp_path, VALID_CONFIG))
    assert config.vote_scout == ScoutFilters()
    assert config.vote_scout.min_tvl_usd == 300_000.0
    assert config.vote_scout.require_major_pair is True


def test_vote_scout_custom_filters(tmp_path):
    content = VALID_CONFIG + textwrap.dedent(
        """
        vote_scout:
          min_tvl_usd: 150000
          require_major_pair: false
          extra_major_symbols: [REI, VIRTUAL]
          max_vote_cv: 0.9
          min_fee_share: 0.05
          min_history: 4
        """
    )
    filters = load_config(write_config(tmp_path, content)).vote_scout
    assert filters.min_tvl_usd == 150_000.0
    assert filters.require_major_pair is False
    assert filters.extra_major_symbols == ("REI", "VIRTUAL")
    assert filters.max_vote_cv == 0.9
    assert filters.min_fee_share == 0.05
    assert filters.min_history == 4


def test_vote_scout_partial_section_keeps_other_defaults(tmp_path):
    content = VALID_CONFIG + "\nvote_scout:\n  min_tvl_usd: 50000\n"
    filters = load_config(write_config(tmp_path, content)).vote_scout
    assert filters.min_tvl_usd == 50_000.0
    assert filters.require_major_pair is True
    assert filters.min_history == 3


@pytest.mark.parametrize(
    "snippet",
    [
        "vote_scout:\n  min_tvl_usd: -5\n",
        "vote_scout:\n  min_tvl_usd: much\n",
        "vote_scout:\n  require_major_pair: sometimes\n",
        "vote_scout:\n  extra_major_symbols: REI\n",
        "vote_scout:\n  min_fee_share: 1.5\n",
        "vote_scout:\n  min_history: 2.5\n",
        "vote_scout:\n  no_such_gate: 1\n",
        "vote_scout: just-a-string\n",
    ],
)
def test_vote_scout_rejects_malformed_settings(tmp_path, snippet):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, VALID_CONFIG + "\n" + snippet))


def test_vote_scout_min_token_age(tmp_path):
    content = VALID_CONFIG + "\nvote_scout:\n  min_token_age_days: 90\n"
    filters = load_config(write_config(tmp_path, content)).vote_scout
    assert filters.min_token_age_days == 90.0
    bad = VALID_CONFIG + "\nvote_scout:\n  min_token_age_days: -1\n"
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, bad))


def test_vote_scout_sustainability_and_safety_settings(tmp_path):
    content = VALID_CONFIG + textwrap.dedent(
        """
        vote_scout:
          min_fees_per_emission: 0.25
          token_safety: false
        """
    )
    filters = load_config(write_config(tmp_path, content)).vote_scout
    assert filters.min_fees_per_emission == 0.25
    assert filters.token_safety is False
    bad = VALID_CONFIG + "\nvote_scout:\n  token_safety: maybe\n"
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, bad))


def test_db_path_env_override_wins_over_config(tmp_path, monkeypatch):
    # the Docker image sets HRUSHA_DB_PATH=/data/hrusha.db so one config.yaml
    # works bare-metal AND mounted — the env var must beat the file
    content = VALID_CONFIG + '\ndb_path: "/somewhere/else.db"\n'
    monkeypatch.setenv("HRUSHA_DB_PATH", str(tmp_path / "data" / "override.db"))
    config = load_config(write_config(tmp_path, content))
    assert config.db_path == tmp_path / "data" / "override.db"
    monkeypatch.setenv("HRUSHA_DB_PATH", "   ")
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, content))
