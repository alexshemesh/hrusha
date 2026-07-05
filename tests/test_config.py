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
