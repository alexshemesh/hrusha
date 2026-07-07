"""USD price resolution at event time, with a daily cache in SQLite.

Resolution order: price_cache -> DefiLlama -> Alchemy Prices API.

DefiLlama (coins.llama.fi, free, keyless) is primary: on the first miss
for a token it fetches the token's ENTIRE daily price history in one
`/chart` call and fills the cache for every day at once — a backfill
costs ~1 request per distinct token instead of one per (token, day).
It prices Base tokens from on-chain DEX pools with no history-depth
limit (CoinGecko's free tier refuses ranges older than 365 days, which
is why it was dropped).

Caching rules: definitive misses (DefiLlama covered the range and the
day has no price, or it doesn't know the token and Alchemy answered
"no data") are cached as NULL so they are looked up at most once per
day. Transient failures (rate limits, outages) are NOT cached —
otherwise a throttled backfill would poison the cache with NULLs for
legit tokens. `usd_at_time` stays NULL on unpriced events and can be
backfilled later (docs/IMPLEMENTATION_PLAN.md, risk register #2).

Prices are daily granularity — acceptable per the design; exactness
matters for coin amounts, USD is analytical.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx

from hrusha.providers.interface import ProviderError

NATIVE_SYMBOL = "ETH"
DEFILLAMA_CHART_URL_TEMPLATE = "https://coins.llama.fi/chart/{coin}"
DEFILLAMA_ETH_COIN = "coingecko:ethereum"
DEFILLAMA_CHAIN = "base"
DEFILLAMA_MAX_SPAN_DAYS = 500  # /chart rejects larger spans
REQUEST_TIMEOUT_SECONDS = 30.0
SECONDS_PER_DAY = 86400
# after this many consecutive provider failures, stop asking it this run:
# each failure costs a full retry cycle, and a throttled provider stays
# throttled — without a breaker a backfill burns minutes on doomed calls
PROVIDER_FAILURE_LIMIT = 3

CHART_OK = "ok"  # history fetched and cached
CHART_MISSING = "missing"  # DefiLlama does not know the token (spam etc.)
CHART_FAILED = "failed"  # transient fetch problem; nothing proven

log = logging.getLogger("hrusha.prices")


class PriceResolver:
    """Caching facade over price sources. `provider` needs historical_usd_price()."""

    def __init__(
        self, conn: sqlite3.Connection, provider, http: httpx.Client | None = None
    ) -> None:
        self._conn = conn
        self._provider = provider
        self._http = http or httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self._provider_failures = 0  # consecutive; trips the circuit breaker
        self._charts: dict[str, tuple[str, int]] = {}  # token -> (state, covered_from_ts)

    def usd_price(self, token_key: str, ts: int) -> Decimal | None:
        """USD price of 'ETH' or a token contract on the UTC day containing `ts`."""
        day = _day(ts)
        found, price = self._cached(token_key, day)
        if found:
            return price

        chart_state = self._chart_state(token_key, ts)
        if chart_state == CHART_OK:
            found, price = self._cached(token_key, day)  # chart fill landed here
            if found:
                return price
            if day != _day(int(time.time())):
                # the chart covered this day yet the cache still misses:
                # the token had no price that day — a definitive miss
                self._store(token_key, day, None)
                log.warning(
                    "no USD price found",
                    extra={"token": token_key, "day": day, "cached_as_miss": True},
                )
                return None
            # TODAY may simply not have a chart point yet — that is not
            # definitive; ask the provider, and never cache a miss for it
            price, _ = self._provider_price(token_key, ts)
            if price is not None:
                self._store(token_key, day, price)
            else:
                log.warning(
                    "no USD price found",
                    extra={"token": token_key, "day": day, "cached_as_miss": False},
                )
            return price

        price, definitive = self._provider_price(token_key, ts)
        definitive = definitive and chart_state == CHART_MISSING
        if price is not None or definitive:
            self._store(token_key, day, price)
        if price is None:
            log.warning(
                "no USD price found",
                extra={"token": token_key, "day": day, "cached_as_miss": definitive},
            )
        return price

    # -- DefiLlama: whole history per token, one request ----------------------

    def _chart_state(self, token_key: str, ts: int) -> str:
        state, covered_from = self._charts.get(token_key, (None, 0))
        if state is None or (state == CHART_OK and ts < covered_from):
            # never fetched, or fetched from a later start (another wallet's
            # first sighting of the token can predate the chart's coverage)
            state = self._fetch_chart(token_key, ts)
            self._charts[token_key] = (state, ts)
        return state

    def _fetch_chart(self, token_key: str, ts: int) -> str:
        coin = _llama_coin(token_key)
        day_start = ts - ts % SECONDS_PER_DAY
        span = min(
            (int(time.time()) - day_start) // SECONDS_PER_DAY + 2,
            DEFILLAMA_MAX_SPAN_DAYS,
        )
        try:
            response = self._http.get(
                DEFILLAMA_CHART_URL_TEMPLATE.format(coin=coin),
                params={"start": day_start, "span": span, "period": "1d"},
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            log.warning(
                "DefiLlama chart failed",
                extra={"token": token_key, "status": exc.response.status_code},
            )
            return CHART_FAILED
        except (httpx.HTTPError, ValueError) as exc:
            log.warning(
                "DefiLlama chart failed",
                extra={"token": token_key, "reason": exc.__class__.__name__},
            )
            return CHART_FAILED
        points = ((body.get("coins") or {}).get(coin) or {}).get("prices") or []
        if not points:
            return CHART_MISSING  # DefiLlama omits unknown tokens entirely
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO price_cache (token, day, usd) VALUES (?, ?, ?)",
                # point timestamps jitter seconds AROUND midnight (23:59:54
                # is common) — flooring would file them under the previous
                # day and leave the intended day a false definitive miss
                [
                    (token_key, _day(_nearest_day_start(p["timestamp"])), float(p["price"]))
                    for p in points
                ],
            )
        log.info(
            "price history cached",
            extra={"token": token_key, "days": len(points), "source": "defillama"},
        )
        return CHART_OK

    # -- Alchemy fallback: per (token, day), behind a circuit breaker ---------

    def _provider_price(self, token_key: str, ts: int) -> tuple[Decimal | None, bool]:
        """Price, plus whether a None answer is definitive (cacheable) rather
        than a transient upstream failure that should retry on the next sync."""
        if self._provider_failures >= PROVIDER_FAILURE_LIMIT:
            return None, False  # breaker open: the provider's answer is unknown
        try:
            price = self._provider.historical_usd_price(token_key, ts)
        except ProviderError:
            log.warning("provider price lookup failed", extra={"token": token_key})
            self._provider_failures += 1
            if self._provider_failures == PROVIDER_FAILURE_LIMIT:
                log.warning("price provider keeps failing; skipping it for the rest of this run")
            return None, False
        self._provider_failures = 0
        return price, True

    # -- cache ----------------------------------------------------------------

    def _cached(self, token_key: str, day: str) -> tuple[bool, Decimal | None]:
        row = self._conn.execute(
            "SELECT usd FROM price_cache WHERE token = ? AND day = ?", (token_key, day)
        ).fetchone()
        if row is None:
            return False, None
        return True, Decimal(str(row[0])) if row[0] is not None else None

    def _store(self, token_key: str, day: str, price: Decimal | None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO price_cache (token, day, usd) VALUES (?, ?, ?)",
                (token_key, day, float(price) if price is not None else None),
            )


@dataclass
class RepriceStats:
    repriced: int = 0
    fees_repriced: int = 0
    still_unpriced: int = 0
    cache_misses_purged: int = 0


def reprice_events(conn: sqlite3.Connection, prices: PriceResolver) -> RepriceStats:
    """Backfill usd_at_time / gas_usd on events that were ingested unpriced.

    Cached NULLs for the affected tokens are purged first: most unpriced
    events date from the era when transient throttling poisoned the cache,
    and a backfill that trusts those NULLs would fix nothing. Priced cache
    rows are untouched, and misses that are still definitive (spam tokens
    DefiLlama has never heard of) simply get re-cached as NULL.
    NFT legs are skipped — they have no fungible price by design.
    """
    stats = RepriceStats()
    with conn:
        stats.cache_misses_purged = conn.execute(
            """
            DELETE FROM price_cache WHERE usd IS NULL AND token IN (
                SELECT DISTINCT COALESCE(contract, token) FROM events
                WHERE usd_at_time IS NULL AND token_id IS NULL
                  AND kind IN ('transfer_in', 'transfer_out')
                UNION SELECT 'ETH' WHERE EXISTS (
                    SELECT 1 FROM events WHERE kind = 'gas_fee' AND gas_usd IS NULL
                )
            )
            """
        ).rowcount
    transfer_rows = conn.execute(
        """
        SELECT id, COALESCE(contract, token), ts, amount_native FROM events
        WHERE usd_at_time IS NULL AND token_id IS NULL
          AND kind IN ('transfer_in', 'transfer_out')
        ORDER BY COALESCE(contract, token), ts
        """
    ).fetchall()
    with conn:
        for event_id, token_key, ts, amount in transfer_rows:
            price = prices.usd_price(token_key, ts)
            if price is None:
                stats.still_unpriced += 1
                continue
            conn.execute(
                "UPDATE events SET usd_at_time = ? WHERE id = ?",
                (float(Decimal(amount) * price), event_id),
            )
            stats.repriced += 1
        for event_id, ts, amount in conn.execute(
            "SELECT id, ts, amount_native FROM events WHERE kind = 'gas_fee' AND gas_usd IS NULL"
        ).fetchall():
            price = prices.usd_price(NATIVE_SYMBOL, ts)
            if price is None:
                stats.still_unpriced += 1
                continue
            usd = float(Decimal(amount) * price)
            conn.execute(
                "UPDATE events SET gas_usd = ?, usd_at_time = ? WHERE id = ?",
                (usd, usd, event_id),
            )
            stats.fees_repriced += 1
    return stats


def _llama_coin(token_key: str) -> str:
    if token_key == NATIVE_SYMBOL:
        return DEFILLAMA_ETH_COIN
    return f"{DEFILLAMA_CHAIN}:{token_key}"


def _nearest_day_start(ts: float) -> int:
    return round(ts / SECONDS_PER_DAY) * SECONDS_PER_DAY


def _day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")
