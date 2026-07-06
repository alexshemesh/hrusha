from decimal import Decimal

import httpx

from hrusha.prices import PROVIDER_FAILURE_LIMIT, PriceResolver
from hrusha.providers.interface import ProviderError
from tests.conftest import TOKEN_CONTRACT

TS = 1_750_000_000
DAY_SECONDS = 86_400


class CountingProvider:
    def __init__(self, price, error: Exception | None = None):
        self.price = price
        self.error = error
        self.calls = 0

    def historical_usd_price(self, token_key, ts):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.price


def chart_response(coin: str, day_prices: dict[int, float]) -> dict:
    return {
        "coins": {coin: {"prices": [{"timestamp": ts, "price": p} for ts, p in day_prices.items()]}}
    }


def llama_http(payload: dict, requests_seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests_seen is not None:
            requests_seen.append(str(request.url))
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


def llama_unknown_token_http():
    return llama_http({"coins": {}})  # DefiLlama omits tokens it cannot price


def offline_http():
    return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))


def test_chart_fills_cache_for_all_days_in_one_request(ledger):
    requests_seen = []
    coin = f"base:{TOKEN_CONTRACT}"
    prices = {TS: 1.0, TS + DAY_SECONDS: 2.0, TS + 2 * DAY_SECONDS: 3.0}
    provider = CountingProvider(None)
    resolver = PriceResolver(
        ledger, provider, http=llama_http(chart_response(coin, prices), requests_seen)
    )

    assert resolver.usd_price(TOKEN_CONTRACT, TS) == Decimal("1.0")
    assert resolver.usd_price(TOKEN_CONTRACT, TS + DAY_SECONDS) == Decimal("2.0")
    assert resolver.usd_price(TOKEN_CONTRACT, TS + 2 * DAY_SECONDS) == Decimal("3.0")
    assert len(requests_seen) == 1  # whole history in one chart call
    assert provider.calls == 0  # Alchemy never consulted


def test_eth_uses_the_coingecko_ethereum_coin_id(ledger):
    requests_seen = []
    resolver = PriceResolver(
        ledger,
        CountingProvider(None),
        http=llama_http(chart_response("coingecko:ethereum", {TS: 3000.0}), requests_seen),
    )
    assert resolver.usd_price("ETH", TS) == Decimal("3000.0")
    assert "coingecko:ethereum" in requests_seen[0]


def test_day_gap_inside_covered_chart_is_a_definitive_miss(ledger):
    coin = f"base:{TOKEN_CONTRACT}"
    provider = CountingProvider(Decimal("9.99"))  # would answer, must not be asked
    resolver = PriceResolver(
        ledger, provider, http=llama_http(chart_response(coin, {TS + DAY_SECONDS: 2.0}))
    )
    assert resolver.usd_price(TOKEN_CONTRACT, TS) is None  # day not in chart
    assert provider.calls == 0
    row = ledger.execute(
        "SELECT usd FROM price_cache WHERE token = ? AND day = '2025-06-15'", (TOKEN_CONTRACT,)
    ).fetchone()
    assert row == (None,)


def test_unknown_token_falls_back_to_provider_and_caches_definitive_miss(ledger):
    provider = CountingProvider(None)  # Alchemy also has no data: definitive
    resolver = PriceResolver(ledger, provider, http=llama_unknown_token_http())
    assert resolver.usd_price(TOKEN_CONTRACT, TS) is None
    assert resolver.usd_price(TOKEN_CONTRACT, TS) is None
    assert provider.calls == 1  # second call served from the NULL cache row
    assert ledger.execute("SELECT COUNT(*) FROM price_cache").fetchone() == (1,)


def test_provider_price_used_when_defillama_lacks_the_token(ledger):
    provider = CountingProvider(Decimal("42"))
    resolver = PriceResolver(ledger, provider, http=llama_unknown_token_http())
    assert resolver.usd_price(TOKEN_CONTRACT, TS) == Decimal("42")
    assert resolver.usd_price(TOKEN_CONTRACT, TS) == Decimal("42")
    assert provider.calls == 1  # cached


def test_transient_failures_are_not_cached(ledger):
    # DefiLlama 500s and Alchemy rate-limited: nothing definitive, retry next time
    provider = CountingProvider(None, error=ProviderError("Alchemy Prices API HTTP 429"))
    resolver = PriceResolver(ledger, provider, http=offline_http())
    assert resolver.usd_price(TOKEN_CONTRACT, TS) is None
    assert resolver.usd_price(TOKEN_CONTRACT, TS) is None
    assert provider.calls == 2  # no poisoned cache row, looked up again
    assert ledger.execute("SELECT COUNT(*) FROM price_cache").fetchone() == (0,)


def test_provider_failure_after_unknown_token_is_not_cached(ledger):
    # DefiLlama not knowing the token proves nothing about the failed provider
    provider = CountingProvider(None, error=ProviderError("Alchemy Prices API HTTP 429"))
    resolver = PriceResolver(ledger, provider, http=llama_unknown_token_http())
    assert resolver.usd_price(TOKEN_CONTRACT, TS) is None
    assert ledger.execute("SELECT COUNT(*) FROM price_cache").fetchone() == (0,)


def test_breaker_stops_asking_failing_provider(ledger):
    provider = CountingProvider(None, error=ProviderError("Alchemy Prices API HTTP 429"))
    resolver = PriceResolver(ledger, provider, http=llama_unknown_token_http())
    for i in range(PROVIDER_FAILURE_LIMIT + 5):
        resolver.usd_price(TOKEN_CONTRACT, TS + i * DAY_SECONDS)  # distinct days
    assert provider.calls == PROVIDER_FAILURE_LIMIT  # breaker tripped


def test_provider_success_resets_breaker(ledger):
    provider = CountingProvider(Decimal("3000"))
    resolver = PriceResolver(ledger, provider, http=llama_unknown_token_http())
    resolver._provider_failures = 2  # two strikes, then a success
    assert resolver.usd_price("ETH", TS) == Decimal("3000")
    assert resolver._provider_failures == 0


def test_chart_refetched_when_an_earlier_day_is_requested(ledger):
    coin = f"base:{TOKEN_CONTRACT}"
    starts_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        starts_seen.append(start)
        history = {TS - 10 * DAY_SECONDS: 5.0, TS: 1.0}
        in_range = {ts: p for ts, p in history.items() if ts >= start}
        return httpx.Response(200, json=chart_response(coin, in_range))

    resolver = PriceResolver(
        ledger,
        CountingProvider(None),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert resolver.usd_price(TOKEN_CONTRACT, TS) == Decimal("1.0")
    # another tracked wallet saw the token 10 days earlier: chart must refetch
    assert resolver.usd_price(TOKEN_CONTRACT, TS - 10 * DAY_SECONDS) == Decimal("5.0")
    assert len(starts_seen) == 2
    assert starts_seen[1] < starts_seen[0]
