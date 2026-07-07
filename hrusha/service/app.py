"""FastAPI dashboard: server-rendered pages over the local ledger.

Phase 5 of docs/IMPLEMENTATION_PLAN.md — no SPA, no client-side state:
every page is a plain HTML render of ledger queries, so anything the
dashboard shows can be reproduced with the CLI.

Security posture: the app is a personal, single-operator tool that
`hrusha serve` binds to 127.0.0.1 by default. There is no auth and no
CSRF protection — do not expose it beyond localhost (or put a
reverse-proxy with auth in front, Phase 6+). Spam-token symbols are
attacker-controlled strings; Jinja autoescaping is relied on everywhere
they render.

The refresh button runs a full sync on a background thread (one at a
time); its outcome is shown on the next page load. Threads open their
own SQLite connection — connections never cross threads.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from hrusha.config import Config
from hrusha.ledger import reports
from hrusha.ledger import tags as tags_module
from hrusha.ledger.store import open_ledger
from hrusha.ledger.tags import SECONDS_PER_WEEK

log = logging.getLogger("hrusha.app")

TEMPLATES_DIR = Path(__file__).parent / "templates"
SECONDS_PER_DAY = 86400
STALE_AFTER_SECONDS = 2 * 3600  # nudge for a refresh when snapshots age


@dataclass
class SyncState:
    """Refresh-button state, shared across requests, guarded by `lock`."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    last_outcome: str = ""  # human-readable one-liner
    last_finished_ts: int = 0


@dataclass
class ScoutState:
    """Vote-scan state, shared across requests, guarded by `lock`.

    The scan is ~3 minutes of chunked eth_calls, so it runs on a
    background thread like refresh; the page renders the last result."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    result: object | None = None  # vote_scout.ScoutResult
    last_error: str = ""


def default_sync_runner(config: Config) -> str:
    """Full sync with the production wiring; returns a one-line outcome."""
    # deferred: pulls web3 + providers, which the read-only pages never need
    from hrusha.adapters.morpho import MorphoAdapter
    from hrusha.cli import make_aerodrome_adapter, make_forty_acres_adapter
    from hrusha.prices import PriceResolver
    from hrusha.providers.alchemy_rpc import AlchemyProvider
    from hrusha.providers.blockscout import BlockscoutProvider
    from hrusha.service.sync import run_full_sync

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
    return (
        f"synced: {summary.transfers.events_inserted} transfers, "
        f"{summary.fees.events_inserted} fees, {summary.balance_snapshots} snapshots"
    )


def default_scout_runner(config: Config):
    """Full Aerodrome vote scan with the production wiring."""
    # deferred: pulls web3, which the read-only pages never need
    from hrusha.service import vote_scout

    return vote_scout.scan(config)


def create_app(config: Config, sync_runner=None, scout_runner=None) -> FastAPI:
    """App factory: explicit wiring, injectable sync/scout for tests."""
    app = FastAPI(title="hrusha", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["usd"] = _fmt_usd
    templates.env.filters["amount"] = _fmt_amount
    templates.env.filters["when"] = _fmt_when
    sync_state = SyncState()
    scout_state = ScoutState()
    run_sync = sync_runner or default_sync_runner
    run_scout = scout_runner or default_scout_runner

    def page(request: Request, name: str, **context):
        context.update(
            sync_running=sync_state.running,
            sync_outcome=sync_state.last_outcome,
        )
        return templates.TemplateResponse(request, name, context)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def overview(request: Request):
        conn = open_ledger(config.db_path)
        try:
            snapshots = reports.latest_snapshots(conn)
            strategies = reports.strategy_summary(conn, snapshots)
        finally:
            conn.close()
        by_kind: dict[str, list[reports.SnapshotRow]] = {}
        for row in snapshots:
            by_kind.setdefault(row.kind, []).append(row)
        as_of = max((row.ts for row in snapshots), default=0)
        now = int(time.time())
        next_flip = now - now % SECONDS_PER_WEEK + SECONDS_PER_WEEK
        return page(
            request,
            "overview.html",
            strategies=strategies,
            balances=by_kind.get("balance", []),
            positions=by_kind.get("position", []),
            claimables=by_kind.get("claimable", []),
            total_balance_usd=_usd_total(by_kind.get("balance", [])),
            total_position_usd=_usd_total(by_kind.get("position", [])),
            total_claimable_usd=_usd_total(by_kind.get("claimable", [])),
            wallet_labels={address: label for label, address in config.addresses.items()},
            as_of=as_of,
            stale=bool(as_of) and now - as_of > STALE_AFTER_SECONDS,
            epoch_flip_in_seconds=next_flip - now,
            epoch_flip_at=next_flip,
        )

    @app.get("/strategy/{source}")
    def strategy(request: Request, source: str):
        conn = open_ledger(config.db_path)
        try:
            snapshots = reports.latest_snapshots(conn)
            row = next(
                (r for r in reports.strategy_summary(conn, snapshots) if r.source == source),
                None,
            )
            epochs = [r for r in reports.neto_by_epoch_source(conn) if r.source == source]
            transfer_rows = reports.recent_transfers(conn, limit=200, source=source)
        finally:
            conn.close()
        if row is None:
            raise HTTPException(status_code=404, detail="no such strategy in the ledger")
        return page(
            request,
            "strategy.html",
            row=row,
            epochs=epochs,
            rows=transfer_rows,
            wallet_labels={address: label for label, address in config.addresses.items()},
        )

    @app.get("/income")
    def income(request: Request, days: int = 90, coins: bool = False):
        days = max(1, min(days, 3650))  # bound untrusted arithmetic like /transfers does
        since_ts = int(time.time()) - days * SECONDS_PER_DAY
        conn = open_ledger(config.db_path)
        try:
            if coins:
                coin_rows = reports.coins_by_epoch_source(conn, since_ts=since_ts)
                return page(request, "income.html", coins=True, coin_rows=coin_rows, days=days)
            rows = reports.neto_by_epoch_source(conn, since_ts=since_ts)
        finally:
            conn.close()
        return page(request, "income.html", coins=False, rows=rows, days=days)

    @app.get("/transfers")
    def transfers(
        request: Request,
        limit: int = 50,
        epoch: str | None = None,
        source: str | None = None,
        tag: str | None = None,
    ):
        limit = max(1, min(limit, 500))
        conn = open_ledger(config.db_path)
        try:
            rows = reports.recent_transfers(
                conn, limit=limit, epoch_id=epoch, source=source, tag=tag
            )
            fees = reports.fee_summary(conn, since_ts=int(time.time()) - 30 * SECONDS_PER_DAY)
        finally:
            conn.close()
        return page(
            request,
            "transfers.html",
            rows=rows,
            fees=fees,
            limit=limit,
            epoch=epoch,
            source=source,
            tag=tag,
            wallet_labels={address: label for label, address in config.addresses.items()},
        )

    @app.post("/tag")
    def tag(request: Request, event_id: int = Form(...), tag: str = Form(...)):
        _reject_cross_site(request)
        tag = tag.strip()
        conn = open_ledger(config.db_path)
        try:
            if tag and not tags_module.set_manual_tag(conn, event_id, tag):
                log.warning("manual tag for unknown event", extra={"event_id": event_id})
        finally:
            conn.close()
        back = request.headers.get("referer") or "/transfers"
        # 303: re-GET the page after the form post
        return RedirectResponse(_same_origin_path(back), status_code=303)

    @app.get("/votes")
    def votes(request: Request):
        now = int(time.time())
        epoch_start = now - now % SECONDS_PER_WEEK
        cutoff = epoch_start + SECONDS_PER_WEEK - 3600  # voting disabled the final hour
        with scout_state.lock:
            result = scout_state.result
            scan_running = scout_state.running
            scan_error = scout_state.last_error
        return page(
            request,
            "votes.html",
            result=result,
            filters=config.vote_scout,
            scan_running=scan_running,
            scan_error=scan_error,
            scan_stale=result is not None and result.epoch_start != epoch_start,
            cutoff_at=cutoff,
            cutoff_in_seconds=max(0, cutoff - now),
        )

    @app.post("/votes/scan")
    def votes_scan(request: Request):
        _reject_cross_site(request)
        with scout_state.lock:
            already_running = scout_state.running
            if not already_running:
                scout_state.running = True
        if not already_running:
            threading.Thread(target=_run_scout_thread, daemon=True).start()
        return RedirectResponse("/votes", status_code=303)

    def _run_scout_thread():
        result, error = None, ""
        try:
            result = run_scout(config)
        except Exception as exc:
            # class name only: provider exceptions can embed the RPC URL (API key)
            log.error("vote scan failed", exc_info=exc)
            error = f"scan failed: {exc.__class__.__name__} (see logs)"
        with scout_state.lock:
            scout_state.running = False
            scout_state.last_error = error
            if result is not None:
                scout_state.result = result

    @app.post("/refresh")
    def refresh(request: Request):
        _reject_cross_site(request)
        with sync_state.lock:
            already_running = sync_state.running
            if not already_running:
                sync_state.running = True
        if not already_running:
            threading.Thread(target=_run_sync_thread, daemon=True).start()
        return RedirectResponse("/", status_code=303)

    def _run_sync_thread():
        try:
            outcome = run_sync(config)
        except Exception as exc:
            log.error("dashboard-triggered sync failed", exc_info=exc)
            outcome = f"sync failed: {exc.__class__.__name__} (see logs)"
        with sync_state.lock:
            sync_state.running = False
            sync_state.last_outcome = outcome
            sync_state.last_finished_ts = int(time.time())

    return app


def _usd_total(rows: list[reports.SnapshotRow]) -> float:
    return sum(row.usd_at_time or 0.0 for row in rows)


def _reject_cross_site(request: Request) -> None:
    """CSRF guard for the mutating routes: a malicious website can make the
    operator's browser form-POST to localhost, so any request that declares
    a web origin must declare OURS. Requests without an Origin header
    (curl, same-app forms in older browsers) pass — they are not the
    drive-by attack this blocks."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    # 'Origin: null' (sandboxed/data: pages) intentionally fails this compare
    host = request.headers.get("host", "")
    if origin.split("://", 1)[-1] != host:
        raise HTTPException(status_code=403, detail="cross-site form post rejected")


def _same_origin_path(url: str) -> str:
    """Reduce a redirect target to its path+query: no cross-origin bounces.

    Anything that could still leave the origin or smuggle headers —
    protocol-relative '//host' paths, backslash tricks, control
    characters — falls back to '/' instead of being sanitized in place.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    path = parts.path or "/"
    target = f"{path}?{parts.query}" if parts.query else path
    if (
        not target.startswith("/")
        or target.startswith("//")
        or "\\" in target
        or any(ord(ch) < 0x20 or ch == "\x7f" for ch in target)
    ):
        return "/"
    return target


def _fmt_usd(value) -> str:
    return f"${value:,.2f}" if value is not None else "?"


def _fmt_amount(value) -> str:
    amount = float(value)
    return f"{amount:,.6f}".rstrip("0").rstrip(".") if amount else "0"


def _fmt_when(ts: int) -> str:
    if not ts:
        return "never"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
