"""Vote scout against the persistent chain-fact cache.

The scan itself needs an RPC node, so these exercise the seams where the
cache is read and written: what a warm cache lets the scan skip, what a
cold one persists, and that a missing ledger only costs speed.
"""

import httpx
import pytest

from hrusha.ledger.chain_cache import ChainCache
from hrusha.service.vote_scout import (
    HISTORY_EPOCHS,
    SECONDS_PER_WEEK,
    _fetch_first_seen,
    _fetch_token_risks,
    _FreshFacts,
    _open_cache_conn,
    _persist_pool_facts,
    _scan_candidate,
)

POOL = "0x" + "a" * 40
TOKEN0, TOKEN1 = "0x" + "b" * 40, "0x" + "c" * 40
EPOCH_START = 100 * SECONDS_PER_WEEK


@pytest.fixture
def cache(ledger):
    return ChainCache(ledger)


def candidate(lp=POOL):
    return {
        "lp": lp,
        "votes": 10.0,
        "bribes": [],
        "fees": [],
        "fees_usd": 100.0,
        "incentives_usd": 0.0,
        "blind_share": 0.0,
        "emissions_rate": 0.0,
        "migrating": False,
    }


class FakeWeb3:
    @staticmethod
    def to_checksum_address(address):
        return address


# -- reading a warm cache ----------------------------------------------------


def test_cached_pool_skips_token_tvl_and_history_reads(monkeypatch):
    """token0/token1/kind and closed epochs are immutable — a warm candidate
    should reach its RawPool without a single eth_call."""
    from hrusha.ledger.chain_cache import PoolMeta
    from hrusha.service import vote_scout

    # TVL still needs balanceOf, so stub the one read the cache can't answer
    monkeypatch.setattr(vote_scout, "_describe_token", lambda *a, **k: ("USDC", 6))

    class TvlOnlyW3:
        class eth:  # noqa: N801
            @staticmethod
            def contract(**kwargs):
                class Contract:
                    class functions:  # noqa: N801
                        @staticmethod
                        def balanceOf(_who):  # noqa: N802 — ERC-20 ABI name
                            return type("Call", (), {"call": staticmethod(lambda: 0)})()

                return Contract()

    meta = PoolMeta(POOL, TOKEN0, TOKEN1, "sAMM", EPOCH_START - SECONDS_PER_WEEK)
    cached_epochs = [(EPOCH_START - SECONDS_PER_WEEK * (i + 1), 5.0, True) for i in range(3)]

    raw, pair, fresh = _scan_candidate(
        candidate(),
        TvlOnlyW3(),
        FakeWeb3,
        rewards_sugar=None,  # epochsByAddress must not be called
        http=None,
        prices={TOKEN0: (1.0, 1.0), TOKEN1: (1.0, 1.0)},
        token_meta={},
        epoch_start=EPOCH_START,
        now=EPOCH_START + 3600,
        aero_price=1.0,
        token_decimals={},
        pool_meta=meta,
        cached_epochs=cached_epochs,
    )

    assert pair == (TOKEN0, TOKEN1)
    assert raw.name.startswith("sAMM-")  # kind came from the cache, not a probe
    assert raw.final_votes == (5.0, 5.0, 5.0)
    assert raw.incentive_epochs == 3
    assert fresh == _FreshFacts()  # nothing new learned, nothing to write back


def test_cached_history_is_capped_to_the_projection_window():
    """A pool accumulates epochs forever; the projection only ever uses the
    most recent HISTORY_EPOCHS of them."""
    from hrusha.ledger.chain_cache import PoolMeta

    meta = PoolMeta(POOL, TOKEN0, TOKEN1, "vAMM", EPOCH_START - SECONDS_PER_WEEK)
    many = [(EPOCH_START - SECONDS_PER_WEEK * (i + 1), float(i), False) for i in range(20)]

    raw, _pair, _fresh = _scan_candidate(
        candidate(),
        _StubW3(),
        FakeWeb3,
        None,
        None,
        {TOKEN0: (1.0, 1.0), TOKEN1: (1.0, 1.0)},
        {},
        EPOCH_START,
        EPOCH_START + 3600,
        1.0,
        {},
        pool_meta=meta,
        cached_epochs=many,
    )
    assert len(raw.final_votes) == HISTORY_EPOCHS


class _StubW3:
    class eth:  # noqa: N801
        @staticmethod
        def contract(**kwargs):
            class Contract:
                class functions:  # noqa: N801
                    @staticmethod
                    def symbol():
                        return type("Call", (), {"call": staticmethod(lambda: "TKN")})()

                    @staticmethod
                    def decimals():
                        return type("Call", (), {"call": staticmethod(lambda: 18)})()

                    @staticmethod
                    def balanceOf(_who):  # noqa: N802 — ERC-20 ABI name
                        return type("Call", (), {"call": staticmethod(lambda: 0)})()

            return Contract()


# -- writing back ------------------------------------------------------------


def test_persist_writes_metadata_before_the_history_marker(cache):
    """store_pool_epochs updates a pool_meta row, so a pool discovered this
    scan must get its metadata row first or the marker lands nowhere."""
    epochs = ((EPOCH_START - SECONDS_PER_WEEK, 5.0, True),)
    _persist_pool_facts(
        cache,
        {POOL: _FreshFacts(pool_meta=(TOKEN0, TOKEN1, "vAMM"), epochs=epochs)},
        EPOCH_START,
    )
    meta = cache.pool_meta(POOL)
    assert (meta.token0, meta.kind) == (TOKEN0, "vAMM")
    assert meta.epochs_synced_ts == EPOCH_START  # complete through the running epoch
    assert cache.pool_epochs(POOL) == [(EPOCH_START - SECONDS_PER_WEEK, 5.0, True)]


def test_persist_is_a_no_op_without_a_cache():
    _persist_pool_facts(None, {POOL: _FreshFacts(pool_meta=(TOKEN0, TOKEN1, "vAMM"))}, EPOCH_START)


# -- GoPlus verdict caching --------------------------------------------------


def test_cached_verdicts_skip_the_http_call(cache):
    risky, clean = "0x" + "1" * 40, "0x" + "2" * 40
    cache.store_token_risks(risky, ["is_honeypot"], now=1000)
    cache.store_token_risks(clean, [], now=1000)

    def explode(request):
        raise AssertionError("cached tokens must not be re-fetched")

    http = httpx.Client(transport=httpx.MockTransport(explode))
    risks, checked = _fetch_token_risks(http, [risky, clean], {}, cache, now=1000)

    assert checked  # no call was needed, so nothing failed
    assert risks[risky] == (f"{risky[:10]}:is_honeypot",)
    assert clean not in risks


def test_fetched_verdicts_are_stored_including_clean_and_unknown(cache):
    risky, clean, unknown = ("0x" + c * 40 for c in "123")

    def handler(request):
        token = request.url.params["contract_addresses"]
        if token == risky:
            return httpx.Response(200, json={"result": {risky: {"is_honeypot": "1"}}})
        if token == clean:
            return httpx.Response(200, json={"result": {clean: {"is_honeypot": "0"}}})
        return httpx.Response(200, json={"result": {}})  # GoPlus never scanned it

    http = httpx.Client(transport=httpx.MockTransport(handler))
    _fetch_token_risks(http, [risky, clean, unknown], {}, cache, now=1000)

    assert cache.token_risks(risky, now=1000) == ("is_honeypot",)
    assert cache.token_risks(clean, now=1000) == ()
    # unknown caches as clean-with-TTL: it is not a risk flag today, and
    # re-asking every scan costs a call per token to learn the same nothing
    assert cache.token_risks(unknown, now=1000) == ()


def test_an_outage_is_not_cached_as_clean(cache):
    token = "0x" + "1" * 40
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    risks, checked = _fetch_token_risks(http, [token], {}, cache, now=1000)
    assert risks == {} and not checked
    assert cache.token_risks(token, now=1000) is None  # still unknown, retried next scan


def test_outage_on_an_empty_token_list_still_counts_as_checked(cache):
    http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    _risks, checked = _fetch_token_risks(http, [], {}, cache, now=1000)
    assert checked  # nothing to check is not an outage


# -- DefiLlama first-seen ----------------------------------------------------


def test_first_seen_caches_hits_but_retries_misses(cache):
    known, unpriced = "0x" + "1" * 40, "0x" + "2" * 40
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"coins": {f"base:{known}": {"timestamp": 1690000000}}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    first = _fetch_first_seen(http, {known, unpriced}, cache)
    assert first == {known: 1690000000}

    # second scan: the hit is cached, the miss is asked again — a token
    # DefiLlama cannot price today may gain a price later
    first = _fetch_first_seen(http, {known, unpriced}, cache)
    assert first == {known: 1690000000}
    assert len(calls) == 2
    assert known not in calls[1] and unpriced in calls[1]


# -- degradation -------------------------------------------------------------


def test_scan_runs_without_a_ledger(tmp_path, caplog):
    """A locked or unreadable ledger costs the scan speed, never the scan."""
    from hrusha.config import Config

    config = Config(
        addresses={"main": "0x" + "9" * 40},
        alchemy_api_key="unused",
        etherscan_api_key=None,
        db_path=tmp_path / "no-such-dir" / "x" / "ledger.db",
    )
    (tmp_path / "no-such-dir").write_text("not a directory")  # mkdir will fail

    assert _open_cache_conn(config) is None
