"""Dashboard pages against a seeded ledger, with an injected sync runner."""

import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hrusha.config import Config
from hrusha.ledger.ingest import ingest_transfers
from hrusha.ledger.store import open_ledger
from hrusha.ledger.tags import retag_all
from hrusha.service.app import create_app
from tests.conftest import COLD, MAIN, make_transfer

SPAM_SYMBOL = "<script>alert(1)</script>"


@pytest.fixture
def config(tmp_path):
    return Config(
        addresses={"main": MAIN, "cold": COLD},
        alchemy_api_key="unused",
        etherscan_api_key=None,
        db_path=Path(tmp_path) / "ledger.db",
    )


def seed(config):
    conn = open_ledger(config.db_path)
    recent = int(time.time()) - 3600  # inside the income page's default window
    ingest_transfers(
        conn,
        [
            make_transfer(token="AERO", amount=Decimal("25"), ts=recent),
            make_transfer(log_index=8, token=SPAM_SYMBOL, amount=Decimal("1"), ts=recent),
        ],
        tracked_addresses={MAIN, COLD},
        price_fn=lambda token, ts: Decimal("2.0"),
    )
    retag_all(conn, {MAIN, COLD})  # assigns epochs; wipes sources, so set ours after
    with conn:
        conn.execute("UPDATE events SET source = 'aerodrome-voting' WHERE token = 'AERO'")
        conn.execute(
            """
            INSERT INTO snapshots (ts, chain, address, kind, token, source,
                                   amount_native, usd_at_time)
            VALUES (?, 'base', ?, 'position', 'AERO', 'aerodrome-voting', '100', 5000.0),
                   (?, 'base', ?, 'claimable', ?, 'aerodrome-voting', '3', 9.0),
                   (?, 'base', ?, 'balance', 'ETH', NULL, '0.5', 1200.0)
            """,
            (
                int(time.time()),
                MAIN,
                int(time.time()),
                MAIN,
                SPAM_SYMBOL,
                int(time.time()),
                MAIN,
            ),
        )
    conn.close()


def make_client(config, sync_runner=None, scout_runner=None, invest_runner=None):
    return TestClient(
        create_app(
            config,
            sync_runner=sync_runner,
            scout_runner=scout_runner,
            invest_runner=invest_runner,
        )
    )


def make_scout_result(pool_name="CL100-WETH/USDC"):
    import time as time_module

    from hrusha.service.vote_scout import RawPool, ScoutResult, VeNft, rank

    now = int(time_module.time())
    epoch_start = now - now % (7 * 24 * 3600)
    raws = [
        RawPool(
            lp="0x" + "a" * 40,
            name=pool_name,
            symbols=("WETH", "USDC"),
            votes=2_000_000.0,
            fees_usd=9_000.0,
            incentives_usd=0.0,
            blind_share=0.0,
            tvl_usd=5_000_000.0,
            final_votes=(4_000_000.0, 4_500_000.0, 4_200_000.0),
        ),
        RawPool(
            lp="0x" + "b" * 40,
            name="vAMM-TRAP/USDC",
            symbols=("TRAP", "USDC"),
            votes=1_000.0,
            fees_usd=5_000.0,
            incentives_usd=0.0,
            blind_share=0.0,
            tvl_usd=1_000.0,  # the $1k trap pool: flagged, never suggested
            final_votes=(900.0, 1_100.0, 1_000.0),
        ),
    ]
    return ScoutResult(
        scanned_at=now,
        epoch_start=epoch_start,
        cutoff_ts=epoch_start + 7 * 24 * 3600 - 3600,
        aero_price=0.5,
        my_power=10_000.0,
        my_value_usd=5_000.0,
        venfts=[VeNft(id=7, power=10_000.0, voted_this_epoch=False, wallet_label="main")],
        pools=rank(raws, 0.5, 10_000.0),
    )


def test_overview_shows_positions_claimables_balances(config):
    seed(config)
    body = make_client(config).get("/").text
    assert "aerodrome-voting" in body
    assert "$5,000.00" in body  # position total
    assert "$1,200.00" in body  # balance total
    assert "epoch flips in" in body


def test_spam_token_symbols_are_escaped_everywhere(config):
    seed(config)
    client = make_client(config)
    for path in ("/", "/transfers"):
        body = client.get(path).text
        assert "<script>" not in body
        assert "&lt;script&gt;" in body


def test_overview_without_data_invites_first_sync(config):
    body = make_client(config).get("/").text
    assert "refresh" in body


def test_overview_shows_strategy_profit_with_detail_links(config):
    seed(config)
    body = make_client(config).get("/").text
    assert "Profit by strategy" in body
    assert "/strategy/aerodrome-voting" in body


def test_strategy_detail_page(config):
    seed(config)
    client = make_client(config)
    body = client.get("/strategy/aerodrome-voting").text
    assert "lifetime profit" in body
    assert "Income by epoch" in body
    assert "$50.00" in body  # the seeded claim income
    assert client.get("/strategy/nope").status_code == 404


def test_income_page_renders_report_and_drilldown_link(config):
    seed(config)
    body = make_client(config).get("/income").text
    assert "aerodrome-voting" in body
    assert "$50.00" in body  # 25 AERO * $2
    assert "/transfers?epoch=" in body


def test_income_coin_view(config):
    seed(config)
    body = make_client(config).get("/income?coins=1").text
    assert "AERO" in body
    assert "25" in body


def test_transfers_filters_by_tag(config):
    seed(config)
    conn = open_ledger(config.db_path)
    from hrusha.ledger.tags import set_manual_tag

    event_id = conn.execute("SELECT id FROM events WHERE token = 'AERO'").fetchone()[0]
    set_manual_tag(conn, event_id, "special")
    conn.close()
    client = make_client(config)
    assert "AERO" in client.get("/transfers?tag=special").text
    assert "AERO" not in client.get("/transfers?tag=nosuch").text


def test_tag_post_persists_manual_tag_and_redirects_same_origin(config):
    seed(config)
    client = make_client(config)
    conn = open_ledger(config.db_path)
    event_id = conn.execute("SELECT id FROM events WHERE token = 'AERO'").fetchone()[0]
    conn.close()

    response = client.post(
        "/tag",
        data={"event_id": event_id, "tag": "checked"},
        headers={"referer": "https://evil.example/phish?x=1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/")  # never bounce off-origin
    assert "evil.example" not in location

    conn = open_ledger(config.db_path)
    tags = conn.execute(
        "SELECT tag, origin FROM tags WHERE event_id = ? AND tag = 'checked'", (event_id,)
    ).fetchall()
    conn.close()
    assert tags == [("checked", "manual")]


def test_cross_site_form_posts_are_rejected(config):
    # a malicious website can make the operator's browser POST to localhost;
    # any request declaring a foreign (or 'null') Origin must be refused
    seed(config)
    client = make_client(config, sync_runner=lambda cfg: "never")
    for origin in ("https://evil.example", "null"):
        for path, data in (
            ("/tag", {"event_id": 1, "tag": "x"}),
            ("/refresh", {}),
            ("/votes/scan", {}),
        ):
            response = client.post(
                path, data=data, headers={"origin": origin}, follow_redirects=False
            )
            assert response.status_code == 403, (path, origin)
    conn = open_ledger(config.db_path)
    assert conn.execute("SELECT COUNT(*) FROM tags WHERE origin='manual'").fetchone() == (0,)
    conn.close()


def test_same_origin_and_originless_posts_still_work(config):
    seed(config)
    client = make_client(config)
    conn = open_ledger(config.db_path)
    event_id = conn.execute("SELECT id FROM events WHERE token = 'AERO'").fetchone()[0]
    conn.close()
    same_origin = client.post(
        "/tag",
        data={"event_id": event_id, "tag": "ok"},
        headers={"origin": "http://testserver"},
        follow_redirects=False,
    )
    assert same_origin.status_code == 303
    no_origin = client.post(  # curl-style client: no Origin header at all
        "/tag", data={"event_id": event_id, "tag": "ok2"}, follow_redirects=False
    )
    assert no_origin.status_code == 303


def test_same_origin_path_neutralizes_smuggling_attempts():
    # httpx refuses to even send CR/LF headers, so exercise the helper directly
    from hrusha.service.app import _same_origin_path

    # urlsplit strips raw CR/LF (Python 3.10+); the guard must ensure no
    # control character ever reaches the Location header either way
    smuggled = _same_origin_path("http://h/a\r\nSet-Cookie:x")
    assert "\r" not in smuggled and "\n" not in smuggled
    assert _same_origin_path("http://h/a\x00b") == "/"  # NUL survives urlsplit
    assert _same_origin_path("http://h/a%0d%0aSet-Cookie:x") == "/a%0d%0aSet-Cookie:x"
    # percent-encoded CRLF stays encoded — the response header carries no raw CR/LF


@pytest.mark.parametrize(
    "referer",
    [
        "http://h//evil.example/p",  # reduces to protocol-relative //host
        "/\\evil.example",  # backslash tricks
        "not-a-url-at-all",
    ],
)
def test_hostile_referers_redirect_to_root(config, referer):
    seed(config)
    client = make_client(config)
    conn = open_ledger(config.db_path)
    event_id = conn.execute("SELECT id FROM events").fetchone()[0]
    conn.close()
    response = client.post(
        "/tag",
        data={"event_id": event_id, "tag": "t"},
        headers={"referer": referer},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_income_days_is_clamped(config):
    seed(config)
    client = make_client(config)
    assert client.get("/income?days=99999999999999").status_code == 200
    assert client.get("/income?days=-5").status_code == 200


def test_refresh_runs_injected_sync_once(config):
    seed(config)
    calls = []

    def fake_sync(cfg):
        calls.append(cfg.db_path)
        return "synced: test"

    client = make_client(config, sync_runner=fake_sync)
    response = client.post("/refresh", follow_redirects=False)
    assert response.status_code == 303

    for _ in range(100):  # background thread: wait for the outcome to land
        if "synced: test" in client.get("/").text:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("sync outcome never appeared")
    assert calls == [config.db_path]


def test_sync_failure_is_reported_not_raised(config):
    seed(config)

    def broken_sync(cfg):
        raise RuntimeError("provider down")

    client = make_client(config, sync_runner=broken_sync)
    client.post("/refresh", follow_redirects=False)
    for _ in range(100):
        body = client.get("/").text
        if "sync failed" in body:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("failure outcome never appeared")


def test_votes_page_before_any_scan_invites_one(config):
    body = make_client(config).get("/votes").text
    assert "scan pools" in body
    assert "vote cutoff in" in body


def test_votes_scan_renders_suggestions_in_percent_and_flags_traps(config):
    client = make_client(config, scout_runner=lambda cfg: make_scout_result())
    assert client.post("/votes/scan", follow_redirects=False).status_code == 303
    for _ in range(100):  # background thread: wait for the result to land
        body = client.get("/votes").text
        if "CL100-WETH/USDC" in body:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("scan result never appeared")
    assert "%" in body and "APR" in body  # profits in percents
    assert "veNFT #7" in body and "no active allocation" in body
    # the $1k trap pool shows in the table with its flag, never as suggested
    assert "LOW-TVL" in body
    suggested_section = body.split("All candidates")[0]
    assert "TRAP" not in suggested_section


def test_votes_page_distinguishes_recast_carried_and_empty_allocations(config):
    from hrusha.service.vote_scout import VeNft

    result = make_scout_result()
    result.venfts = [
        VeNft(1, 100.0, True, "main", active_pool_count=1),
        VeNft(2, 200.0, False, "main", active_pool_count=2),
        VeNft(3, 300.0, False, "main", active_pool_count=0),
    ]
    client = make_client(config, scout_runner=lambda cfg: result)
    client.post("/votes/scan", follow_redirects=False)
    for _ in range(100):
        body = client.get("/votes").text
        if "veNFT #3" in body:
            break
        time.sleep(0.02)
    assert "recast this epoch ✓" in body
    assert "carried forward — 2 pools" in body
    assert "no active allocation" in body
    assert "NOT voted" not in body


def test_votes_scan_failure_is_reported_not_raised(config):
    def broken_scout(cfg):
        raise RuntimeError("rpc down")

    client = make_client(config, scout_runner=broken_scout)
    client.post("/votes/scan", follow_redirects=False)
    for _ in range(100):
        body = client.get("/votes").text
        if "scan failed" in body:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("failure outcome never appeared")
    assert "rpc down" not in body  # exception text may embed the RPC URL/key


def test_votes_page_escapes_chain_derived_pool_names(config):
    client = make_client(config, scout_runner=lambda cfg: make_scout_result(pool_name=SPAM_SYMBOL))
    client.post("/votes/scan", follow_redirects=False)
    for _ in range(100):
        body = client.get("/votes").text
        if "scanned" in body:
            break
        time.sleep(0.02)
    assert "<script>" not in body
    assert "&lt;script&gt;" in body


def make_invest_result(*, liquidity_checked=True):
    from hrusha.service.invest_scout import (
        IdleBalance,
        InvestResult,
        OpportunityScore,
        RawOpportunity,
    )

    return InvestResult(
        scanned_at=int(time.time()),
        balances=[
            IdleBalance(token="USDC", amount=393.33, usd_value=393.33),
            IdleBalance(token="ETH", amount=0.0002, usd_value=0.06),
            IdleBalance(token="cbBTC", amount=0.00002, usd_value=2.47),
        ],
        opportunities=[
            OpportunityScore(
                raw=RawOpportunity(
                    protocol="aerodrome-slipstream",
                    mechanism="stable-lp",
                    token="USDC",
                    supply_apy_pct=49.15,
                    available_liquidity_usd=12_000_000.0,
                    total_supply_usd=20_000_000.0,
                ),
                risk_tags=("il-risk", "slipstream-concentrated", "utilization-not-checked"),
                withdrawal_safe=True,
                notes=("best safe APY for USDC",),
            ),
            OpportunityScore(
                raw=RawOpportunity(
                    protocol="aave-v3",
                    mechanism="lending",
                    token="USDC",
                    supply_apy_pct=4.0,
                    available_liquidity_usd=500_000_000.0,
                    total_supply_usd=600_000_000.0,
                    utilization=0.85,
                ),
                risk_tags=("aave-utilization-high",),
                withdrawal_safe=True,
            ),
            OpportunityScore(
                raw=RawOpportunity(
                    protocol="morpho",
                    mechanism="lending-vault",
                    token="USDC",
                    supply_apy_pct=0.0,
                    available_liquidity_usd=1_000_000.0,
                ),
                risk_tags=("zero-apy",),
                withdrawal_safe=False,
            ),
        ],
        liquidity_checked=liquidity_checked,
    )


def test_invest_scan_renders_idle_balances_and_risk_tags(config):
    client = make_client(config, invest_runner=lambda cfg: make_invest_result())
    assert client.post("/invest/scan", follow_redirects=False).status_code == 303
    for _ in range(100):
        body = client.get("/invest").text
        if "USDC" in body and "Idle balances" in body:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("invest scan result never appeared")
    # idle balances rendered
    assert "USDC" in body and "393.33" in body
    # best safe option shows the high-APY aerodrome pick, not the 0% morpho one
    best = body.split("All opportunities")[0]
    assert "aerodrome-slipstream" in best and "49.15" in best
    assert "morpho" not in best
    # full table shows every option, nothing hidden — even the 0% / not-safe ones
    assert "morpho" in body and "zero-apy" in body
    assert "il-risk" in body and "utilization-not-checked" in body


def test_invest_scan_failure_is_reported_not_raised(config):
    def broken_invest(cfg):
        raise RuntimeError("defillama down")

    client = make_client(config, invest_runner=broken_invest)
    client.post("/invest/scan", follow_redirects=False)
    for _ in range(100):
        body = client.get("/invest").text
        if "scan failed" in body:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("failure outcome never appeared")
    assert "defillama down" not in body  # exception text not leaked to the page


def test_invest_page_warns_when_liquidity_source_unreachable(config):
    client = make_client(
        config, invest_runner=lambda cfg: make_invest_result(liquidity_checked=False)
    )
    client.post("/invest/scan", follow_redirects=False)
    for _ in range(100):
        body = client.get("/invest").text
        if "liquidity data source" in body:
            break
        time.sleep(0.02)
    assert "unreachable" in body


def test_health(config):
    assert make_client(config).get("/health").json() == {"status": "ok"}


def test_api_docs_are_disabled(config):
    client = make_client(config)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_votes_page_renders_informational_notes_on_suggested_pools(config):
    from hrusha.service.vote_scout import RawPool, rank

    def scout_with_note(cfg):
        result = make_scout_result()
        raws = [
            RawPool(
                lp="0x" + "d" * 40,
                # majors pair so the default EXOTIC-PAIR gate doesn't hide it:
                # this test is about the NOTE rendering on a suggested pool
                name="vAMM-WETH/USDC",
                symbols=("WETH", "USDC"),
                votes=2_000_000.0,
                fees_usd=9_000.0,
                incentives_usd=0.0,
                blind_share=0.0,
                tvl_usd=5_000_000.0,
                final_votes=(4_000_000.0, 4_500_000.0, 4_200_000.0),
                self_bribe_share=1.0,
            )
        ]
        result.pools = rank(raws, 0.5, 10_000.0)
        return result

    client = make_client(config, scout_runner=scout_with_note)
    client.post("/votes/scan", follow_redirects=False)
    for _ in range(100):
        body = client.get("/votes").text
        if "vAMM-WETH/USDC" in body:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("scan result never appeared")
    suggested_section = body.split("All candidates")[0]
    assert "SELF-BRIBED(100%)" in suggested_section  # visible on a SUGGESTED pool
