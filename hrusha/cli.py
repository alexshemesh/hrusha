"""hrusha command-line interface.

Phase 0 ships `hrusha sync --dry-run`: read the config, connect to
Alchemy, print native ETH balances per configured address. The full
sync (transfers, fees, ledger writes) arrives with Phase 1.

Exit codes: 0 ok, 2 config problem, 3 provider problem.
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid

from hrusha import __version__
from hrusha.config import Config, ConfigError, load_config
from hrusha.logs import setup_logging
from hrusha.providers.alchemy_rpc import ProviderError, fetch_eth_balances

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_PROVIDER_ERROR = 3

log = logging.getLogger("hrusha.cli")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    if args.command == "sync":
        return run_sync(dry_run=args.dry_run)
    parser.error(f"unknown command: {args.command}")
    return EXIT_CONFIG_ERROR  # unreachable; parser.error raises SystemExit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hrusha",
        description="Personal crypto income monitor on Base.",
    )
    parser.add_argument("--version", action="version", version=f"hrusha {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug-level logs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser("sync", help="fetch on-chain state into the local ledger")
    sync.add_argument(
        "--dry-run",
        action="store_true",
        help="read config, connect to Alchemy, print ETH balances; write nothing",
    )
    return parser


def run_sync(dry_run: bool) -> int:
    sync_run_id = uuid.uuid4().hex[:12]
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if not dry_run:
        print(
            "error: full sync arrives with Phase 1 (docs/IMPLEMENTATION_PLAN.md); "
            "use `hrusha sync --dry-run`",
            file=sys.stderr,
        )
        return EXIT_CONFIG_ERROR

    log.info(
        "dry-run sync started",
        extra={"sync_run_id": sync_run_id, "address_count": len(config.addresses)},
    )
    try:
        balances = fetch_eth_balances(config.alchemy_api_key, config.addresses)
    except ProviderError as exc:
        log.error("provider call failed", extra={"sync_run_id": sync_run_id})
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER_ERROR

    print_balances(config, balances)
    log.info("dry-run sync finished", extra={"sync_run_id": sync_run_id})
    return EXIT_OK


def print_balances(config: Config, balances: dict[str, object]) -> None:
    label_width = max(len(label) for label in balances)
    print(f"ETH balances on Base ({len(balances)} addresses):")
    for label, amount in balances.items():
        address = config.addresses[label]
        print(f"  {label:<{label_width}}  {address}  {amount:.6f} ETH")


if __name__ == "__main__":
    sys.exit(main())
