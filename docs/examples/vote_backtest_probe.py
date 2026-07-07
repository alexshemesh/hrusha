"""Vote scout back-test: would the suggested pools' profits have paid off?

Read-only probe — no writes, no state. For each pool address given on
the command line it reconstructs the last N completed epochs from
RewardsSugar.epochsByAddress (final votes and final rewards are known
facts there) and values each epoch's rewards at CLAIM-TIME prices via
DefiLlama's historical endpoint. Then it replays the scout's model
out-of-sample: for each epoch E, "predicted $/1k votes" uses only
epochs before E (median of the prior 6, same as the live scout), and
is compared with what E actually paid.

Finally it simulates "vote the scout's top pick every epoch" with the
operator's current voting power and puts that next to the actual
aerodrome-voting income recorded in the ledger for the same epochs.

Run:  .venv/bin/python docs/examples/vote_backtest_probe.py <pool> [<pool>...]
      (pool addresses come from the /votes page or vote_scout_probe.py)

Caveats printed with the result: ledger income lands when a claim tx
happens (often a later epoch than the vote), and the simulation holds
today's voting power constant across history.
"""

from __future__ import annotations

import statistics
import sys
import time
from datetime import UTC, datetime

import httpx
from web3 import Web3

from hrusha.adapters.known_contracts import REWARDS_SUGAR
from hrusha.config import load_config
from hrusha.ledger.store import open_ledger

# the scout's ABI + scoring live in the service module now; reuse, don't fork
from hrusha.service.vote_scout import (
    ERC20_ABI,
    HISTORY_EPOCHS,
    POOL_ABI,
    REWARDS_SUGAR_ABI,
    SECONDS_PER_WEEK,
    WEI,
    _pool_kind,
)

DEFILLAMA_HISTORICAL_URL = "https://coins.llama.fi/prices/historical/{ts}/{coins}"
EVAL_EPOCHS = 12  # epochs judged; needs HISTORY_EPOCHS more of run-up before them
PRICE_BATCH = 40


def day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def fetch_historical_prices(http: httpx.Client, ts: int, tokens: set[str]) -> dict[str, float]:
    prices: dict[str, float] = {}
    todo = sorted(tokens)
    for start in range(0, len(todo), PRICE_BATCH):
        coins = ",".join(f"base:{t}" for t in todo[start : start + PRICE_BATCH])
        response = http.get(
            DEFILLAMA_HISTORICAL_URL.format(ts=ts, coins=coins), params={"searchWidth": "24h"}
        )
        response.raise_for_status()
        for coin, data in (response.json().get("coins") or {}).items():
            if data.get("price") is not None:
                prices[coin.split(":", 1)[1].lower()] = float(data["price"])
    return prices


def main() -> None:
    pool_addresses = sys.argv[1:]
    if not pool_addresses:
        raise SystemExit("usage: vote_backtest_probe.py <pool-address> [<pool-address>...]")
    config = load_config()
    w3 = Web3(Web3.HTTPProvider(f"https://base-mainnet.g.alchemy.com/v2/{config.alchemy_api_key}"))
    http = httpx.Client(timeout=30)
    rewards_sugar = w3.eth.contract(
        address=Web3.to_checksum_address(REWARDS_SUGAR), abi=REWARDS_SUGAR_ABI
    )
    now = int(time.time())
    epoch_start = now // SECONDS_PER_WEEK * SECONDS_PER_WEEK

    token_decimals: dict[str, int] = {}

    def decimals_of(token: str) -> int:
        token = token.lower()
        if token not in token_decimals:
            erc20 = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
            try:
                token_decimals[token] = erc20.functions.decimals().call()
            except Exception:  # noqa: BLE001 — odd token; 18 is the overwhelming default
                token_decimals[token] = 18
        return token_decimals[token]

    # -- pull each pool's completed-epoch history ------------------------------
    pools: list[dict] = []
    for address in pool_addresses:
        lp = Web3.to_checksum_address(address)
        pool = w3.eth.contract(address=lp, abi=POOL_ABI)
        symbols = []
        for fn in ("token0", "token1"):
            token = getattr(pool.functions, fn)().call()
            erc20 = w3.eth.contract(address=token, abi=ERC20_ABI)
            symbols.append(erc20.functions.symbol().call())
        name = f"{_pool_kind(pool)}-{'/'.join(symbols)}"
        rows = rewards_sugar.functions.epochsByAddress(
            EVAL_EPOCHS + HISTORY_EPOCHS + 1, 0, lp
        ).call()
        epochs = [
            {
                "ts": ts,
                "votes": votes / WEI,
                "legs": [*bribes, *fees],
            }
            for ts, _lp, votes, _em, bribes, fees in rows
            if ts < epoch_start  # completed epochs only
        ]
        epochs.sort(key=lambda e: e["ts"])  # oldest first for the walk-forward
        pools.append({"lp": lp, "name": name, "epochs": epochs})
        print(f"{name}: {len(epochs)} completed epochs "
              f"({day(epochs[0]['ts'])} .. {day(epochs[-1]['ts'])})")

    # -- value every epoch's rewards at claim-time (epoch flip) prices ---------
    by_claim_ts: dict[int, set[str]] = {}
    for p in pools:
        for e in p["epochs"]:
            by_claim_ts.setdefault(e["ts"] + SECONDS_PER_WEEK, set()).update(
                token.lower() for token, _ in e["legs"]
            )
    prices_at: dict[int, dict[str, float]] = {}
    for claim_ts, tokens in sorted(by_claim_ts.items()):
        prices_at[claim_ts] = fetch_historical_prices(http, min(claim_ts, now), tokens)
    for p in pools:
        for e in p["epochs"]:
            prices = prices_at[e["ts"] + SECONDS_PER_WEEK]
            e["reward_usd"] = sum(
                amount / 10 ** decimals_of(token) * prices.get(token.lower(), 0.0)
                for token, amount in e["legs"]
            )
            e["usd_per_1k"] = e["reward_usd"] / e["votes"] * 1000 if e["votes"] else 0.0

    # -- walk-forward: predict each epoch only from what preceded it -----------
    for p in pools:
        for index, e in enumerate(p["epochs"]):
            prior = p["epochs"][max(0, index - HISTORY_EPOCHS) : index]
            if len(prior) >= 3:
                projected_votes = statistics.median(x["votes"] for x in prior)
                predicted_reward = statistics.median(x["reward_usd"] for x in prior)
                e["predicted_per_1k"] = (
                    predicted_reward / projected_votes * 1000 if projected_votes else 0.0
                )
            else:
                e["predicted_per_1k"] = None

    print(f"\nper-epoch outcomes, last {EVAL_EPOCHS} completed epochs "
          f"(rewards valued at claim-day prices):")
    for p in pools:
        rows = [e for e in p["epochs"] if e["predicted_per_1k"] is not None][-EVAL_EPOCHS:]
        actuals = [e["usd_per_1k"] for e in rows]
        errors = [
            abs(e["usd_per_1k"] - e["predicted_per_1k"]) / e["usd_per_1k"] * 100
            for e in rows
            if e["usd_per_1k"]
        ]
        print(f"\n== {p['name']}  ({p['lp']})")
        print(f"{'epoch':<12} {'votes(M)':>9} {'reward$':>10} {'$/1k act':>9} {'$/1k pred':>10}")
        for e in rows:
            print(f"{day(e['ts']):<12} {e['votes'] / 1e6:>9.2f} {e['reward_usd']:>10,.0f} "
                  f"{e['usd_per_1k']:>9.2f} {e['predicted_per_1k']:>10.2f}")
        if actuals:
            print(f"   median actual ${statistics.median(actuals):.2f}/1k, "
                  f"worst ${min(actuals):.2f}, best ${max(actuals):.2f}, "
                  f"median |prediction error| "
                  f"{statistics.median(errors):.0f}%" if errors else "   no priced epochs")

    # -- strategy sim: top predicted pick each epoch, realized outcome ---------
    eval_ts = sorted(
        {e["ts"] for p in pools for e in p["epochs"] if e["predicted_per_1k"] is not None}
    )[-EVAL_EPOCHS:]
    my_power = 33_041.0  # TODO(alex): current power; history not reconstructed
    print(f"\nstrategy sim — vote ALL {my_power:,.0f} power on the best PREDICTED pool "
          f"each epoch, collect its ACTUAL payout:")
    total = 0.0
    for ts in eval_ts:
        candidates = [
            (e["predicted_per_1k"], e["usd_per_1k"], p["name"])
            for p in pools
            for e in p["epochs"]
            if e["ts"] == ts and e["predicted_per_1k"] is not None
        ]
        if not candidates:
            continue
        predicted, actual, name = max(candidates)
        earned = actual * my_power / 1000
        total += earned
        print(f"  {day(ts)}: {name:<22} predicted ${predicted:>6.2f}/1k, "
              f"actual ${actual:>6.2f}/1k -> ${earned:,.2f}")
    print(f"  simulated total over {len(eval_ts)} epochs: ${total:,.2f}")

    # -- what actually happened, per the ledger --------------------------------
    conn = open_ledger(config.db_path)
    try:
        rows = conn.execute(
            """
            SELECT epoch_id, SUM(usd_at_time) FROM events
            WHERE source = 'aerodrome-voting' AND kind = 'transfer_in'
              AND EXISTS (SELECT 1 FROM tags WHERE event_id = events.id AND tag = 'claim')
            GROUP BY epoch_id ORDER BY epoch_id DESC LIMIT ?
            """,
            (EVAL_EPOCHS,),
        ).fetchall()
    finally:
        conn.close()
    actual_total = sum(usd or 0.0 for _, usd in rows)
    print(f"\nledger says your ACTUAL aerodrome-voting claim income over the last "
          f"{len(rows)} epochs with claims: ${actual_total:,.2f}")
    for epoch_id, usd in rows:
        print(f"  {epoch_id}: ${usd or 0.0:,.2f}")
    print("\ncaveats: claims land in the epoch you CLAIM, not the epoch you earned;")
    print("the sim holds today's voting power constant across history; rewards are")
    print("valued at claim-day prices, your claims at their actual event-day prices")


if __name__ == "__main__":
    main()
