"""Tests for the agent-queryable ledger: `query_events`, `hrusha query`, GET /query.

Uses synthetic addresses/hashes only (see conftest).
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hrusha.config import Config
from hrusha.ledger.ingest import ingest_transfers
from hrusha.ledger.reports import query_events
from hrusha.ledger.store import open_ledger
from hrusha.ledger.tags import retag_all
from hrusha.service.app import create_app

from .conftest import COLD, MAIN, TS_1, TX_1, make_transfer


def _seed(conn, *, tracked=(MAIN, COLD)):
    ingest_transfers(
        conn,
        [
            make_transfer(token="AERO", amount=Decimal("25"), ts=TS_1),
            make_transfer(log_index=8, token="USDC", amount=Decimal("10"), ts=TS_1 + 100),
            make_transfer(
                log_index=9,
                direction="out",
                token="AERO",
                amount=Decimal("5"),
                ts=TS_1 + 200,
                tx_hash=TX_1,
            ),
        ],
        tracked_addresses=set(tracked),
        price_fn=lambda token, ts: Decimal("2.0"),
    )
    retag_all(conn, set(tracked))
    with conn:
        conn.execute("UPDATE events SET source = 'aerodrome-voting' WHERE token = 'AERO'")


@pytest.fixture
def conn(tmp_path):
    c = open_ledger(Path(tmp_path) / "ledger.db")
    _seed(c)
    yield c
    c.close()


# --- query_events -------------------------------------------------------------


def test_query_returns_rows_newest_first(conn):
    rows = query_events(conn, limit=10)
    assert len(rows) == 3
    assert rows[0]["ts"] >= rows[-1]["ts"]


def test_query_filter_token(conn):
    rows = query_events(conn, token="aero")
    assert rows and all(r["token"] == "AERO" for r in rows)
    assert len(rows) == 2


def test_query_filter_kind(conn):
    rows = query_events(conn, kind="transfer_in")
    assert rows and all(r["kind"] == "transfer_in" for r in rows)
    assert len(rows) == 2


def test_query_filter_address(conn):
    rows = query_events(conn, address=MAIN.lower())
    assert rows and all(r["address"] == MAIN for r in rows)


def test_query_filter_source(conn):
    rows = query_events(conn, source="aerodrome-voting")
    assert rows and all(r["source"] == "aerodrome-voting" for r in rows)


def test_query_since_until(conn):
    rows = query_events(conn, since=TS_1 + 100, until=TS_1 + 200)
    assert len(rows) == 2
    assert all(TS_1 + 100 <= r["ts"] <= TS_1 + 200 for r in rows)


def test_query_limit_clamped(conn):
    rows = query_events(conn, limit=10000)
    assert len(rows) <= 500  # _QUERY_MAX_LIMIT


def test_query_no_match(conn):
    assert query_events(conn, token="NOPE") == []


def test_query_rows_json_serializable(conn):
    rows = query_events(conn, limit=2)
    json.dumps(rows)  # must not raise


# --- CLI ----------------------------------------------------------------------


@pytest.fixture
def config(tmp_path):
    cfg = Config(
        addresses={"main": MAIN, "cold": COLD},
        alchemy_api_key="unused",
        etherscan_api_key=None,
        db_path=Path(tmp_path) / "ledger.db",
    )
    c = open_ledger(cfg.db_path)
    _seed(c)
    c.close()
    return cfg


def test_cli_query_outputs_json(config, capsys):
    from hrusha.cli import main

    rc = main(["query", "--token", "aero", "--limit", "5"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out.strip())
    assert rows and all(r["token"] == "AERO" for r in rows)


def test_cli_query_no_filters(config, capsys):
    from hrusha.cli import main

    rc = main(["query", "--limit", "2"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out.strip())
    assert len(rows) == 2


# --- HTTP GET /query ----------------------------------------------------------


@pytest.fixture
def client(config):
    return TestClient(create_app(config))


def test_http_query_returns_json_array(client):
    r = client.get("/query", params={"token": "aero", "limit": 10})
    assert r.status_code == 200
    rows = r.json()
    assert rows and all(row["token"] == "AERO" for row in rows)


def test_http_query_default_limit(client):
    r = client.get("/query")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_http_query_since_until(client):
    r = client.get("/query", params={"since": TS_1 + 100, "until": TS_1 + 200})
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert all(TS_1 + 100 <= row["ts"] <= TS_1 + 200 for row in rows)


def test_http_query_no_match(client):
    r = client.get("/query", params={"token": "NOPE"})
    assert r.status_code == 200
    assert r.json() == []
