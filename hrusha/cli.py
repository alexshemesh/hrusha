"""hrusha command-line interface.

Commands (Phases 1-2):
  sync            full sync: transfers, fees, tagging, snapshots -> SQLite
  sync --dry-run  read config, connect to Alchemy, print ETH balances only
  balances        live token balances with USD values (not from the ledger)
  transfers       recent ledger transfers with tags
  fees            gas spent, total and per period
  report          neto per epoch x source (USD; --coins for native amounts)
  tag             manually tag an event by id (always wins over rules)
  retag           re-run tag rules + epoch assignment over the whole ledger

Exit codes: 0 ok, 2 config problem, 3 provider problem, 4 bad reference.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from datetime import UTC, datetime

from hrusha import __version__
from hrusha.adapters.aerodrome import AerodromeAdapter
from hrusha.adapters.forty_acres import FortyAcresAdapter
from hrusha.adapters.known_contracts import seed_default_rules
from hrusha.adapters.morpho import MorphoAdapter
from hrusha.config import Config, ConfigError, load_config
from hrusha.ledger import reports
from hrusha.ledger import tags as tags_module
from hrusha.ledger.store import open_ledger
from hrusha.logs import setup_logging
from hrusha.prices import PriceResolver
from hrusha.providers.alchemy_rpc import AlchemyProvider, ProviderError, fetch_eth_balances
from hrusha.providers.blockscout import BlockscoutProvider
from hrusha.service.sync import run_full_sync

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_PROVIDER_ERROR = 3
EXIT_NOT_FOUND = 4

SECONDS_PER_DAY = 86400

log = logging.getLogger("hrusha.cli")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    try:
        return run_command(args, config)
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_PROVIDER_ERROR


def run_command(args: argparse.Namespace, config: Config) -> int:
    if args.command == "sync" and args.dry_run:
        return run_dry_run(config)
    if args.command == "sync":
        return run_sync(config)
    if args.command == "balances":
        return run_balances(config)
    if args.command == "transfers":
        return run_transfers(config, limit=args.limit)
    if args.command == "fees":
        return run_fees(config, days=args.days)
    if args.command == "report":
        return run_report(
            config,
            days=args.days,
            coins=args.coins,
            date_from=args.date_from,
            date_to=args.date_to,
        )
    if args.command == "tag":
        return run_tag(config, event_id=args.event_id, tag=args.tag)
    if args.command == "retag":
        return run_retag(config)
    raise AssertionError(f"unhandled command: {args.command}")


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

    balances = subparsers.add_parser("balances", help="live token balances with USD values")
    del balances  # no extra arguments

    transfers = subparsers.add_parser("transfers", help="recent transfers from the ledger")
    transfers.add_argument("--limit", type=int, default=25, help="rows to show (default 25)")

    fees = subparsers.add_parser("fees", help="gas fees paid, from the ledger")
    fees.add_argument("--days", type=int, default=30, help="look-back window (default 30)")

    report = subparsers.add_parser("report", help="neto per epoch x source")
    report.add_argument("--days", type=int, default=90, help="look-back window (default 90)")
    report.add_argument(
        "--from",
        dest="date_from",
        metavar="YYYY-MM-DD",
        help="period start (UTC, overrides --days)",
    )
    report.add_argument(
        "--to", dest="date_to", metavar="YYYY-MM-DD", help="period end (UTC, exclusive)"
    )
    report.add_argument("--coins", action="store_true", help="native coin amounts, not USD")

    tag = subparsers.add_parser("tag", help="manually tag an event (see ids in `transfers`)")
    tag.add_argument("event_id", type=int)
    tag.add_argument("tag")

    retag = subparsers.add_parser("retag", help="re-run tag rules and epoch assignment")
    del retag  # no extra arguments
    return parser


def run_dry_run(config: Config) -> int:
    balances = fetch_eth_balances(config.alchemy_api_key, config.addresses)
    label_width = max(len(label) for label in balances)
    print(f"ETH balances on Base ({len(balances)} addresses):")
    for label, amount in balances.items():
        print(f"  {label:<{label_width}}  {config.addresses[label]}  {amount:.6f} ETH")
    return EXIT_OK


def run_sync(config: Config) -> int:
    sync_run_id = uuid.uuid4().hex[:12]
    log.info("full sync starting", extra={"sync_run_id": sync_run_id})
    provider = AlchemyProvider(config.alchemy_api_key)
    conn = open_ledger(config.db_path)
    try:
        summary = run_full_sync(
            config,
            provider,
            conn,
            PriceResolver(conn, provider),
            transfer_source=BlockscoutProvider(),
            aerodrome=make_aerodrome_adapter(config),
            morpho=MorphoAdapter(),
            forty_acres=make_forty_acres_adapter(config),
        )
    finally:
        conn.close()
    print(
        f"sync {summary.sync_run_id}: "
        f"{summary.transfers.events_inserted} transfers ingested "
        f"({summary.transfers.own_transfers_tagged} own-transfers), "
        f"{summary.fees.events_inserted} fee events, "
        f"{summary.transfers.events_skipped + summary.fees.events_skipped} duplicates skipped, "
        f"{summary.balance_snapshots} balance snapshots, "
        f"{summary.aerodrome_snapshots} aerodrome snapshots, "
        f"{summary.morpho_snapshots} morpho snapshots, "
        f"{summary.forty_acres_snapshots} 40acres snapshots"
    )
    return EXIT_OK


def _base_w3(config: Config):
    from web3 import Web3  # deferred: web3 import costs ~0.5s, only sync needs it

    rpc_url = f"https://base-mainnet.g.alchemy.com/v2/{config.alchemy_api_key}"
    return Web3(Web3.HTTPProvider(rpc_url))


def make_aerodrome_adapter(config: Config) -> AerodromeAdapter:
    return AerodromeAdapter(_base_w3(config))


def make_forty_acres_adapter(config: Config) -> FortyAcresAdapter:
    return FortyAcresAdapter(_base_w3(config))


def run_balances(config: Config) -> int:
    provider = AlchemyProvider(config.alchemy_api_key)
    balances = provider.balances(config.addresses)
    label_by_address = {address: label for label, address in config.addresses.items()}
    total_usd = 0.0
    print(f"{'wallet':<10} {'token':<12} {'amount':>24} {'USD':>14}")
    for b in sorted(balances, key=lambda b: -(float(b.usd_value or 0))):
        usd = f"{float(b.usd_value):>14,.2f}" if b.usd_value is not None else f"{'?':>14}"
        total_usd += float(b.usd_value or 0)
        wallet = label_by_address.get(b.address, b.address[:8])
        print(f"{wallet:<10} {b.token[:12]:<12} {b.amount:>24,.6f} {usd}")
    print(f"{'total':<10} {'':<12} {'':>24} {total_usd:>14,.2f}")
    return EXIT_OK


def run_transfers(config: Config, limit: int) -> int:
    conn = open_ledger(config.db_path)
    try:
        rows = reports.recent_transfers(conn, limit=limit)
    finally:
        conn.close()
    if not rows:
        print("no transfers in the ledger yet — run `hrusha sync` first")
        return EXIT_OK
    label_by_address = {address: label for label, address in config.addresses.items()}
    print(
        f"{'id':>6} {'when (UTC)':<17} {'dir':<4} {'wallet':<8} {'token':<10} "
        f"{'amount':>18} {'USD':>12}  {'source':<18} tags"
    )
    for row in rows:
        when = datetime.fromtimestamp(row.ts, tz=UTC).strftime("%Y-%m-%d %H:%M")
        direction = "in" if row.kind == "transfer_in" else "out"
        usd = f"{row.usd_at_time:>12,.2f}" if row.usd_at_time is not None else f"{'?':>12}"
        wallet = label_by_address.get(row.address, row.address[:8])
        print(
            f"{row.id:>6} {when:<17} {direction:<4} {wallet:<8} {row.token[:10]:<10} "
            f"{float(row.amount_native):>18,.6f} {usd}  {(row.source or ''):<18} {row.tags}"
        )
    return EXIT_OK


def run_report(
    config: Config,
    days: int,
    coins: bool,
    date_from: str | None = None,
    date_to: str | None = None,
) -> int:
    try:
        since_ts = (
            _parse_utc_date(date_from) if date_from else int(time.time()) - days * SECONDS_PER_DAY
        )
        until_ts = _parse_utc_date(date_to) if date_to else None
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    conn = open_ledger(config.db_path)
    try:
        if coins:
            rows = reports.coins_by_epoch_source(conn, since_ts=since_ts, until_ts=until_ts)
            if not rows:
                print("no events in the window — run `hrusha sync` first")
                return EXIT_OK
            print(f"{'epoch':<12} {'source':<18} {'token':<12} {'dir':<4} {'amount':>24}")
            for epoch_id, source, token, direction, amount in rows:
                print(
                    f"{epoch_id:<12} {source:<18} {token[:12]:<12} {direction:<4} "
                    f"{float(amount):>24,.6f}"
                )
            return EXIT_OK
        rows = reports.neto_by_epoch_source(conn, since_ts=since_ts, until_ts=until_ts)
    finally:
        conn.close()
    if not rows:
        print("no events in the window — run `hrusha sync` first")
        return EXIT_OK
    print(
        f"{'epoch':<12} {'source':<18} {'income':>12} {'spend':>12} "
        f"{'gas':>10} {'neto':>12}  unpriced"
    )
    for row in rows:
        print(
            f"{row.epoch_id:<12} {row.source:<18} {row.income_usd:>12,.2f} "
            f"{row.spend_usd:>12,.2f} {row.gas_usd:>10,.2f} {row.neto_usd:>12,.2f}"
            f"  {row.unpriced_count or ''}"
        )
    print(
        "neto = income - gas, USD at event time; "
        "own-transfers, swap legs and lock/unlock moves excluded"
    )
    return EXIT_OK


def _parse_utc_date(raw: str) -> int:
    try:
        return int(datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    except ValueError:
        raise ValueError(f"invalid date {raw!r}; expected YYYY-MM-DD") from None


def run_tag(config: Config, event_id: int, tag: str) -> int:
    conn = open_ledger(config.db_path)
    try:
        if not tags_module.set_manual_tag(conn, event_id, tag):
            print(f"error: no event with id {event_id}", file=sys.stderr)
            return EXIT_NOT_FOUND
    finally:
        conn.close()
    print(f"event {event_id} tagged {tag!r} (manual, survives retag)")
    return EXIT_OK


def run_retag(config: Config) -> int:
    conn = open_ledger(config.db_path)
    try:
        seed_default_rules(conn)
        stats = tags_module.retag_all(conn, set(config.addresses.values()))
    finally:
        conn.close()
    print(
        f"retag: {stats.rules_run} rules run, {stats.tags_applied} tags applied, "
        f"{stats.sources_set} sources set, {stats.epochs_assigned} epochs assigned"
    )
    return EXIT_OK


def run_fees(config: Config, days: int) -> int:
    conn = open_ledger(config.db_path)
    try:
        summary = reports.fee_summary(conn, since_ts=int(time.time()) - days * SECONDS_PER_DAY)
    finally:
        conn.close()
    print(
        f"gas over the last {days} days: {summary.tx_count} txs, "
        f"{summary.total_eth} ETH, ${summary.total_usd:,.2f}"
    )
    if summary.unpriced_count:
        print(f"note: {summary.unpriced_count} fee events have no USD price yet")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
