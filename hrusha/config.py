"""Load and validate the operator's private config from ~/.hrusha/config.yaml.

The config file lives outside the repository and is never committed.
Error messages name the missing or invalid key but never echo values,
so a misplaced secret cannot leak into logs or CI output.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("~/.hrusha/config.yaml")
DEFAULT_DB_PATH = Path("~/.hrusha/hrusha.db")
CONFIG_PATH_ENV_VAR = "HRUSHA_CONFIG"

_ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


class ConfigError(Exception):
    """The config file is missing or malformed. Messages never echo values."""


@dataclass(frozen=True)
class Config:
    addresses: dict[str, str]  # label -> lowercased 0x address
    alchemy_api_key: str
    etherscan_api_key: str | None
    db_path: Path


def config_path() -> Path:
    raw = os.environ.get(CONFIG_PATH_ENV_VAR)
    return Path(raw).expanduser() if raw else DEFAULT_CONFIG_PATH.expanduser()


def load_config(path: Path | None = None) -> Config:
    path = path if path is not None else config_path()
    if not path.is_file():
        raise ConfigError(
            f"config file not found: {path} — create it manually (it must never be "
            "committed); see README.md for the expected layout"
        )
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" near line {mark.line + 1}" if mark else ""
        # deliberately not str(exc): YAML errors quote file content
        raise ConfigError(f"config file is not valid YAML{location}: {path}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping (see README.md)")

    return Config(
        addresses=_validated_addresses(raw),
        alchemy_api_key=_required_api_key(raw, section="alchemy"),
        etherscan_api_key=_optional_api_key(raw, section="etherscan"),
        db_path=_db_path(raw),
    )


def _validated_addresses(raw: dict) -> dict[str, str]:
    addresses = raw.get("addresses")
    if not isinstance(addresses, dict) or not addresses:
        raise ConfigError(
            "missing or empty required key: addresses (mapping of label -> 0x address)"
        )
    validated: dict[str, str] = {}
    for label, value in addresses.items():
        if not isinstance(value, str) or not _ADDRESS_PATTERN.match(value):
            raise ConfigError(
                f"addresses.{label} is not a 0x hex address of 40 hex chars (value not shown)"
            )
        validated[str(label)] = value.lower()
    return validated


def _required_api_key(raw: dict, section: str) -> str:
    key = _optional_api_key(raw, section)
    if key is None:
        raise ConfigError(f"missing required key: {section}.api_key")
    return key


def _optional_api_key(raw: dict, section: str) -> str | None:
    block = raw.get(section)
    if block is None:
        return None
    if not isinstance(block, dict):
        raise ConfigError(f"config key {section} must be a mapping with an api_key entry")
    key = block.get("api_key")
    if key is None:
        return None
    if not isinstance(key, str) or not key.strip():
        raise ConfigError(f"config key {section}.api_key must be a non-empty string")
    return key.strip()


def _db_path(raw: dict) -> Path:
    value = raw.get("db_path", str(DEFAULT_DB_PATH))
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("config key db_path must be a non-empty path string")
    return Path(value).expanduser()
