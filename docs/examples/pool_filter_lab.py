"""Filter lab: derive the vote scout's risk gates from data, not judgment.

Read-only probe. The scout ships with two judgment-call gates — TVL >=
$300k and majors-only pairs — that the operator wants tested against
reality: does pool size actually predict payout stability, and does the
majors gate cost more upside (REI, WAI, ...) than the risk it removes?

Method: take every alive gauge currently paying >= $500/epoch, pull ~18
completed epochs each (final votes + rewards, valued at claim-day
prices), then measure per pool:

  cv        — coefficient of variation of realized $/1k votes (payout
              stability; lower = steadier)
  ratio     — realized / walk-forward target per epoch (promise-keeping;
              1.0 = paid exactly what the scout's model projected)
  p10 ratio — the 10th percentile of that ratio (tail risk: how bad the
              shortfall gets in a bad week)

Aggregated by CURRENT TVL bucket and by pair class (majors / exotic),
plus rank correlations, plus a 12-epoch walk-forward simulation of
"vote the best predicted pool each week" under each gate variant:
ungated, TVL-only, majors-only, both (the shipped scout), and a
hindsight oracle as the ceiling.

Caveat printed with the results: TVL is measured TODAY — a pool that
grew into its size gets its past stability credited to its present
bucket. Directionally useful, not econometrics.

Run:  .venv/bin/python docs/examples/pool_filter_lab.py   (~5-8 min)
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from datetime import UTC, datetime

import httpx
from web3 import Web3

from hrusha.adapters.known_contracts import AERODROME_FACTORY_REGISTRY, REWARDS_SUGAR
from hrusha.config import load_config
from hrusha.service.vote_scout import (
    ERC20_ABI,
    FACTORY_ABI,
    HISTORY_EPOCHS,
    MAJOR_SYMBOLS,
    POOL_ABI,
    POOL_INDEXES_PER_CALL,
    REGISTRY_ABI,
    REWARDS_SUGAR_ABI,
    SECONDS_PER_WEEK,
    WEI,
    _fetch_prices,
    _pool_kind,
)

DEFILLAMA_HISTORICAL_URL = "https://coins.llama.fi/prices/historical/{ts}/{coins}"
DEFILLAMA_FIRST_URL = "https://coins.llama.fi/prices/first/{coins}"
PRICE_BATCH = 40
REWARD_FLOOR_USD = 500.0  # universe: pools currently paying at least this
UNIVERSE_CAP = 120
LOOKBACK_EPOCHS = 18  # completed epochs pulled per pool
SIM_EPOCHS = 12  # walk-forward simulation window
TVL_BUCKETS = ((0, "<$100k"), (100_000, "$100k-300k"), (300_000, "$300k-1M"),
               (1_000_000, "$1M-5M"), (5_000_000, ">$5M"))
MY_POWER = 33_041.0  # used only to translate $/1k into personal dollars


def day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def tvl_bucket(tvl: float) -> str:
    label = TVL_BUCKETS[0][1]
    for floor, name in TVL_BUCKETS:
        if tvl >= floor:
            label = name
    return label


def fetch_historical_prices(http: httpx.Client, ts: int, tokens: set[str]) -> dict[str, float]:
    prices: dict[str, float] = {}
    todo = sorted(tokens)
    for start in range(0, len(todo), PRICE_BATCH):
        coins = ",".join(f"base:{t}" for t in todo[start : start + PRICE_BATCH])
        response = http.get(
            DEFILLAMA_HISTORICAL_URL.format(ts=ts, coins=coins),
            params={"searchWidth": "24h"},
            timeout=30,
        )
        response.raise_for_status()
        for coin, data in (response.json().get("coins") or {}).items():
            if data.get("price") is not None:
                prices[coin.split(":", 1)[1].lower()] = float(data["price"])
    return prices


def fetch_first_seen(http: httpx.Client, tokens: set[str]) -> dict[str, int]:
    """token -> unix ts of DefiLlama's FIRST recorded price (token age proxy)."""
    first: dict[str, int] = {}
    todo = sorted(tokens)
    for start in range(0, len(todo), PRICE_BATCH):
        coins = ",".join(f"base:{t}" for t in todo[start : start + PRICE_BATCH])
        response = http.get(DEFILLAMA_FIRST_URL.format(coins=coins), timeout=30)
        response.raise_for_status()
        for coin, data in (response.json().get("coins") or {}).items():
            if data.get("timestamp"):
                first[coin.split(":", 1)[1].lower()] = int(data["timestamp"])
    return first


def spearman(xs: list[float], ys: list[float]) -> float:
    """Rank correlation, ties broken by order — good enough for a lab."""
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        rank = [0.0] * len(values)
        for position, index in enumerate(order):
            rank[index] = float(position)
        return rank

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    sx = sum((a - mx) ** 2 for a in rx) ** 0.5
    sy = sum((b - my) ** 2 for b in ry) ** 0.5
    return cov / (sx * sy) if sx and sy else 0.0


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(q * (len(ordered) - 1))))
    return ordered[index]


def main() -> None:
    config = load_config()
    w3 = Web3(Web3.HTTPProvider(f"https://base-mainnet.g.alchemy.com/v2/{config.alchemy_api_key}"))
    http = httpx.Client(timeout=30)
    now = int(time.time())
    epoch_start = now // SECONDS_PER_WEEK * SECONDS_PER_WEEK

    rewards_sugar = w3.eth.contract(
        address=Web3.to_checksum_address(REWARDS_SUGAR), abi=REWARDS_SUGAR_ABI
    )
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(AERODROME_FACTORY_REGISTRY), abi=REGISTRY_ABI
    )
    pool_count = sum(
        w3.eth.contract(address=factory, abi=FACTORY_ABI).functions.allPoolsLength().call()
        for factory in registry.functions.poolFactories().call()
    )
    print(f"scanning {pool_count} pool indexes for the universe...")
    universe: list[dict] = []
    seen: set[str] = set()
    for offset in range(0, pool_count, POOL_INDEXES_PER_CALL):
        rows = rewards_sugar.functions.epochsLatest(POOL_INDEXES_PER_CALL, offset).call()
        for ts, lp, votes, _em, bribes, fees in rows:
            if ts != epoch_start:
                raise SystemExit(f"LpEpoch decode looks wrong: ts={ts} != {epoch_start}")
            if lp.lower() not in seen:
                seen.add(lp.lower())
                universe.append({"lp": lp, "votes": votes / WEI, "legs": [*bribes, *fees]})

    current_prices = _fetch_prices(
        http, {t.lower() for p in universe for t, _ in p["legs"]}
    )
    token_decimals: dict[str, int] = {}
    token_symbols: dict[str, str] = {}

    def describe(token: str) -> tuple[str, int]:
        token = token.lower()
        if token not in token_decimals:
            erc20 = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
            try:
                token_symbols[token] = erc20.functions.symbol().call()
                token_decimals[token] = erc20.functions.decimals().call()
            except Exception:  # noqa: BLE001 — odd token
                token_symbols[token], token_decimals[token] = token[:10], 18
        return token_symbols[token], token_decimals[token]

    for p in universe:
        p["reward_now"] = sum(
            amount / 10 ** describe(token)[1] * current_prices.get(token.lower(), (0.0, 0.0))[0]
            for token, amount in p["legs"]
        )
    universe.sort(key=lambda p: -p["reward_now"])
    pools = [p for p in universe if p["reward_now"] >= REWARD_FLOOR_USD][:UNIVERSE_CAP]
    print(f"universe: {len(pools)} pools currently paying >= ${REWARD_FLOOR_USD:,.0f}/epoch")

    # -- per-pool facts: pair, TVL now, reward history --------------------------
    for index, p in enumerate(pools):
        lp = Web3.to_checksum_address(p["lp"])
        pool = w3.eth.contract(address=lp, abi=POOL_ABI)
        try:
            token0, token1 = pool.functions.token0().call(), pool.functions.token1().call()
        except Exception:  # noqa: BLE001 — nonstandard pool: skip from the lab
            p["skip"] = True
            continue
        current_prices.update(
            _fetch_prices(http, {token0.lower(), token1.lower()} - set(current_prices))
        )
        tvl, symbols = 0.0, []
        for token in (token0, token1):
            symbol, decimals = describe(token)
            symbols.append(symbol)
            erc20 = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
            balance = erc20.functions.balanceOf(lp).call() / 10**decimals
            tvl += balance * current_prices.get(token.lower(), (0.0, 0.0))[0]
        p["name"] = f"{_pool_kind(pool)}-{'/'.join(symbols)}"
        p["major"] = set(symbols) <= MAJOR_SYMBOLS
        p["tvl"] = tvl
        p["pair_tokens"] = (token0.lower(), token1.lower())
        rows = rewards_sugar.functions.epochsByAddress(LOOKBACK_EPOCHS + 1, 0, lp).call()
        p["history"] = sorted(
            (
                {"ts": ts, "votes": votes / WEI, "legs": [*bribes, *fees]}
                for ts, _lp, votes, _em, bribes, fees in rows
                if ts < epoch_start
            ),
            key=lambda e: e["ts"],
        )
        if index % 20 == 19:
            print(f"  deep-looked {index + 1}/{len(pools)} pools")
    pools = [p for p in pools if not p.get("skip") and p["history"]]

    # token age: DefiLlama's first recorded price is a listing-age proxy;
    # a token DefiLlama has never priced gets age 0 (brand new or spam)
    first_seen = fetch_first_seen(http, {t for p in pools for t in p["pair_tokens"]})
    for p in pools:
        ages = [(now - first_seen.get(t, now)) / 86400 for t in p["pair_tokens"]]
        p["min_age_days"] = min(ages)

    # -- value history at claim-day prices, then walk-forward targets -----------
    by_claim_ts: dict[int, set[str]] = defaultdict(set)
    for p in pools:
        for e in p["history"]:
            by_claim_ts[e["ts"] + SECONDS_PER_WEEK].update(t.lower() for t, _ in e["legs"])
    print(f"pricing rewards across {len(by_claim_ts)} claim days...")
    prices_at = {
        ts: fetch_historical_prices(http, min(ts, now), tokens)
        for ts, tokens in sorted(by_claim_ts.items())
    }
    for p in pools:
        for e in p["history"]:
            prices = prices_at[e["ts"] + SECONDS_PER_WEEK]
            e["reward_usd"] = sum(
                amount / 10 ** describe(token)[1] * prices.get(token.lower(), 0.0)
                for token, amount in e["legs"]
            )
            e["per1k"] = e["reward_usd"] / e["votes"] * 1000 if e["votes"] else 0.0
        for index, e in enumerate(p["history"]):
            prior = p["history"][max(0, index - HISTORY_EPOCHS) : index]
            e["target"] = (
                statistics.median(x["per1k"] for x in prior) if len(prior) >= 3 else None
            )

        judged = [e for e in p["history"] if e["target"]]
        per1k = [e["per1k"] for e in p["history"] if e["votes"]]
        p["cv"] = (
            statistics.pstdev(per1k) / statistics.mean(per1k)
            if len(per1k) >= 4 and statistics.mean(per1k) > 0
            else None
        )
        ratios = [e["per1k"] / e["target"] for e in judged if e["target"]]
        p["median_ratio"] = statistics.median(ratios) if ratios else None
        p["p10_ratio"] = percentile(ratios, 0.10) if len(ratios) >= 5 else None
        p["median_per1k"] = statistics.median(per1k) if per1k else 0.0

    measured = [p for p in pools if p["cv"] is not None and p["p10_ratio"] is not None]
    print(f"measured {len(measured)} pools with enough history\n")

    # -- question 1: does size predict stability? -------------------------------
    def bucket_table(groups: dict[str, list[dict]], title: str) -> None:
        print(f"{title}")
        print(f"{'group':<14} {'pools':>5} {'med $/1k':>9} {'med CV':>7} "
              f"{'med ratio':>9} {'p10 ratio':>9}")
        for name, members in groups.items():
            if not members:
                continue
            print(f"{name:<14} {len(members):>5} "
                  f"{statistics.median(m['median_per1k'] for m in members):>9.2f} "
                  f"{statistics.median(m['cv'] for m in members):>7.2f} "
                  f"{statistics.median(m['median_ratio'] for m in members):>9.2f} "
                  f"{statistics.median(m['p10_ratio'] for m in members):>9.2f}")
        print()

    by_tvl: dict[str, list[dict]] = {name: [] for _, name in TVL_BUCKETS}
    for p in measured:
        by_tvl[tvl_bucket(p["tvl"])].append(p)
    bucket_table(by_tvl, "stability by CURRENT TVL bucket "
                         "(CV = payout volatility, ratio = realized/target):")

    print(f"rank correlation TVL vs payout CV: "
          f"{spearman([p['tvl'] for p in measured], [p['cv'] for p in measured]):+.2f}")
    print(f"rank correlation TVL vs p10 ratio: "
          f"{spearman([p['tvl'] for p in measured], [p['p10_ratio'] for p in measured]):+.2f}")
    print(f"rank correlation TVL vs median $/1k: "
          f"{spearman([p['tvl'] for p in measured], [p['median_per1k'] for p in measured]):+.2f}\n")

    # -- question 2: what does the majors gate cost? ----------------------------
    groups = {
        "majors": [p for p in measured if p["major"]],
        "exotic": [p for p in measured if not p["major"]],
        "exotic>=300k": [p for p in measured if not p["major"] and p["tvl"] >= 300_000],
        "exotic<300k": [p for p in measured if not p["major"] and p["tvl"] < 300_000],
    }
    bucket_table(groups, "stability by pair class:")

    # -- question 3: are NEW tokens always toxic? -------------------------------
    # age = days since DefiLlama first priced the pool's YOUNGEST token;
    # majors are all old, so the exotic-only view is the telling one
    age_groups = {
        "<30d": [p for p in measured if p["min_age_days"] < 30],
        "30-90d": [p for p in measured if 30 <= p["min_age_days"] < 90],
        "90-365d": [p for p in measured if 90 <= p["min_age_days"] < 365],
        ">=365d": [p for p in measured if p["min_age_days"] >= 365],
    }
    bucket_table(age_groups, "stability by YOUNGEST pair token age (all pools):")
    exotic_age = {
        f"exotic {name}": [p for p in members if not p["major"]]
        for name, members in age_groups.items()
    }
    bucket_table(exotic_age, "stability by youngest token age (exotic pairs only):")
    exotics = [p for p in measured if not p["major"]]
    if len(exotics) >= 8:
        print(f"rank correlation (exotics) token age vs payout CV: "
              f"{spearman([p['min_age_days'] for p in exotics], [p['cv'] for p in exotics]):+.2f}")
        print(f"rank correlation (exotics) token age vs p10 ratio: "
              f"{spearman([p['min_age_days'] for p in exotics],
                          [p['p10_ratio'] for p in exotics]):+.2f}\n")

    # -- walk-forward simulation under each gate variant ------------------------
    sim_ts = sorted({e["ts"] for p in pools for e in p["history"] if e["target"]})[-SIM_EPOCHS:]
    variants = {
        "ungated": lambda p: True,
        "tvl>=300k": lambda p: p["tvl"] >= 300_000,
        "majors": lambda p: p["major"],
        "both (scout)": lambda p: p["major"] and p["tvl"] >= 300_000,
        "age>=90d": lambda p: p["min_age_days"] >= 90,
        "age90+tvl300k": lambda p: p["min_age_days"] >= 90 and p["tvl"] >= 300_000,
    }
    print(f"simulation, last {len(sim_ts)} epochs — vote best PREDICTED pool each week, "
          f"realize its ACTUAL $/1k (personal $ at {MY_POWER:,.0f} votes):")
    print(f"{'variant':<14} {'total $/1k':>10} {'personal $':>10} {'worst week':>10} "
          f"{'weekly CV':>9}  weeks won by exotic")
    for name, allowed in variants.items():
        weekly = []
        exotic_weeks = 0
        for ts in sim_ts:
            candidates = [
                (e["target"], e["per1k"], p)
                for p in pools
                if allowed(p)
                for e in p["history"]
                if e["ts"] == ts and e["target"]
            ]
            if not candidates:
                continue
            _, actual, chosen = max(candidates, key=lambda c: c[0])
            weekly.append(actual)
            exotic_weeks += not chosen["major"]
        total = sum(weekly)
        cv = statistics.pstdev(weekly) / statistics.mean(weekly) if weekly else 0.0
        print(f"{name:<14} {total:>10.2f} {total * MY_POWER / 1000:>10,.2f} "
              f"{min(weekly):>10.2f} {cv:>9.2f}  {exotic_weeks}")
    oracle = []
    for ts in sim_ts:
        actuals = [e["per1k"] for p in pools for e in p["history"] if e["ts"] == ts and e["target"]]
        if actuals:
            oracle.append(max(actuals))
    print(f"{'oracle':<14} {sum(oracle):>10.2f} {sum(oracle) * MY_POWER / 1000:>10,.2f} "
          f"{min(oracle):>10.2f}  (hindsight ceiling)")

    print("\ncaveats: TVL is today's — survivors that grew get their past credited to")
    print("their present bucket; the universe is pools paying today (survivorship);")
    print("gate checks use today's TVL/pair for past epochs")


if __name__ == "__main__":
    main()
