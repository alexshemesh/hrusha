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
class ScoutFilters:
    """Vote-scout risk gates, operator-tunable via the vote_scout section.

    Defaults match the shipped gates; docs/examples/pool_filter_lab.py is
    the tool for re-deriving them from realized epoch data."""

    min_tvl_usd: float = 300_000.0
    require_major_pair: bool = True
    extra_major_symbols: tuple[str, ...] = ()  # operator-trusted beyond the builtin set
    max_vote_cv: float = 0.6
    min_fee_share: float = 0.10
    min_history: int = 3
    # 0 disables the gate; when set, pools whose YOUNGEST pair token was first
    # priced by DefiLlama fewer than this many days ago are flagged YOUNG-TOKEN
    min_token_age_days: float = 0.0
    # 0 disables; pools whose accrued fees are below this fraction of the AERO
    # emitted to their gauge (same window) get an INFORMATIONAL note
    # (EMISSIONS-SUBSIDIZED, never blocks): vAPR rented, not earned
    min_fees_per_emission: float = 0.1
    # GoPlus mechanical token checks (honeypot, tax walls, pausable transfers)
    # on non-major pair + bribe tokens; the /votes page warns if the check
    # was enabled but unreachable
    token_safety: bool = True


@dataclass(frozen=True)
class Config:
    addresses: dict[str, str]  # label -> lowercased 0x address
    alchemy_api_key: str
    etherscan_api_key: str | None
    db_path: Path
    vote_scout: ScoutFilters = ScoutFilters()


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
        vote_scout=_scout_filters(raw),
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


def _scout_filters(raw: dict) -> ScoutFilters:
    block = raw.get("vote_scout")
    if block is None:
        return ScoutFilters()
    if not isinstance(block, dict):
        raise ConfigError("config key vote_scout must be a mapping of filter settings")
    defaults = ScoutFilters()
    known = {
        "min_tvl_usd",
        "require_major_pair",
        "extra_major_symbols",
        "max_vote_cv",
        "min_fee_share",
        "min_history",
        "min_token_age_days",
        "min_fees_per_emission",
        "token_safety",
    }
    for key in block:
        if key not in known:
            raise ConfigError(f"unknown vote_scout key: {key} (known: {', '.join(sorted(known))})")

    def number(key: str, default: float, upper: float | None = None) -> float:
        value = block.get(key, default)
        if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"vote_scout.{key} must be a non-negative number")
        if upper is not None and value > upper:
            raise ConfigError(f"vote_scout.{key} must be <= {upper}")
        return float(value)

    require_major = block.get("require_major_pair", defaults.require_major_pair)
    if not isinstance(require_major, bool):
        raise ConfigError("vote_scout.require_major_pair must be true or false")
    token_safety = block.get("token_safety", defaults.token_safety)
    if not isinstance(token_safety, bool):
        raise ConfigError("vote_scout.token_safety must be true or false")
    extra = block.get("extra_major_symbols", [])
    if not isinstance(extra, list) or not all(isinstance(s, str) and s.strip() for s in extra):
        raise ConfigError("vote_scout.extra_major_symbols must be a list of token symbols")
    min_history = block.get("min_history", defaults.min_history)
    if not isinstance(min_history, int) or isinstance(min_history, bool) or min_history < 0:
        raise ConfigError("vote_scout.min_history must be a non-negative integer")
    return ScoutFilters(
        min_tvl_usd=number("min_tvl_usd", defaults.min_tvl_usd),
        require_major_pair=require_major,
        extra_major_symbols=tuple(s.strip() for s in extra),
        max_vote_cv=number("max_vote_cv", defaults.max_vote_cv),
        min_fee_share=number("min_fee_share", defaults.min_fee_share, upper=1.0),
        min_history=min_history,
        min_token_age_days=number("min_token_age_days", defaults.min_token_age_days),
        min_fees_per_emission=number("min_fees_per_emission", defaults.min_fees_per_emission),
        token_safety=token_safety,
    )


def _db_path(raw: dict) -> Path:
    value = raw.get("db_path", str(DEFAULT_DB_PATH))
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("config key db_path must be a non-empty path string")
    return Path(value).expanduser()
