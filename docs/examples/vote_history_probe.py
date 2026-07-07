"""Vote report card: did my historical votes hit their targets?

Read-only probe — no writes, no state. It reconstructs the operator's
standing votes at every epoch close from the Voter contract's `Voted` /
`Abstained` events (votes persist across epochs until re-cast or
reset, so "standing at close" is what the epoch actually paid), then
cross-references each voted pool's realized epoch outcome:

  target  = walk-forward prediction at vote time (median of the pool's
            prior 6 completed epochs' $/1k votes — the same number the
            vote scout would have shown)
  reality = the epoch's final rewards valued at claim-day prices,
            split by my weight / final votes

Run:  .venv/bin/python docs/examples/vote_history_probe.py

Event log source is Blockscout's etherscan-compatible logs API (the
project's canonical transfer-history provider); reward history and
ABIs are shared with the vote scout service module.
"""

from __future__ import annotations

import statistics
import time
from collections import defaultdict
from datetime import UTC, datetime

import httpx
from web3 import Web3

from hrusha.adapters.known_contracts import AERODROME_VOTER, REWARDS_SUGAR, VOTING_ESCROW
from hrusha.config import load_config
from hrusha.ledger.store import open_ledger
from hrusha.service.vote_scout import (
    ERC20_ABI,
    HISTORY_EPOCHS,
    POOL_ABI,
    REWARDS_SUGAR_ABI,
    SECONDS_PER_WEEK,
    WEI,
    _pool_kind,
)

BLOCKSCOUT_LOGS_URL = "https://base.blockscout.com/api"
DEFILLAMA_HISTORICAL_URL = "https://coins.llama.fi/prices/historical/{ts}/{coins}"
PRICE_BATCH = 40
HIT_THRESHOLD = 0.8  # realized >= 80% of target counts as a hit

# NB: HexBytes.hex() returns WITHOUT the 0x prefix here, and Blockscout
# silently ignores a malformed topic filter (returning every event for the
# address) — the 0x must be explicit or Abstained rows decode as votes
VOTED_TOPIC = "0x" + Web3.keccak(
    text="Voted(address,address,uint256,uint256,uint256,uint256)"
).hex().removeprefix("0x")
ABSTAINED_TOPIC = "0x" + Web3.keccak(
    text="Abstained(address,address,uint256,uint256,uint256,uint256)"
).hex().removeprefix("0x")


def day(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


LOG_CHUNK_BLOCKS = 2_000_000  # unbounded topic scans time Blockscout out


def fetch_vote_events(
    http: httpx.Client, wallet: str, from_block: int, to_block: int
) -> dict[str, list[dict]]:
    """{'voted': [...], 'abstained': [...]} for this wallet from Blockscout.

    Deliberately filters by topic1 (the voter — rare) ONLY and classifies
    Voted vs Abstained client-side: a server-side topic0 filter forces
    Blockscout onto a slow plan (Voted events number in the millions
    chain-wide) that times out, while topic1 alone answers in seconds.
    Chunked by block range as a further backstop."""
    events: dict[str, list[dict]] = {"voted": [], "abstained": []}
    kind_by_topic = {VOTED_TOPIC.lower(): "voted", ABSTAINED_TOPIC.lower(): "abstained"}
    for start in range(from_block, to_block + 1, LOG_CHUNK_BLOCKS):
        params = {
            "module": "logs",
            "action": "getLogs",
            "fromBlock": start,
            "toBlock": min(start + LOG_CHUNK_BLOCKS - 1, to_block),
            "address": AERODROME_VOTER,
            "topic1": "0x" + wallet.lower().removeprefix("0x").rjust(64, "0"),
        }
        try:
            response = http.get(BLOCKSCOUT_LOGS_URL, params=params, timeout=90)
        except httpx.TimeoutException:  # one retry: Blockscout warms its index
            response = http.get(BLOCKSCOUT_LOGS_URL, params=params, timeout=90)
        response.raise_for_status()
        result = response.json().get("result") or []
        if not isinstance(result, list):
            continue  # etherscan-compat API signals "no records" with a string
        if len(result) >= 1000:
            print(f"warning: {wallet[:10]}… hit the 1000-log page cap; history may be truncated")
        for row in result:
            kind = kind_by_topic.get(row["topics"][0].lower())
            if kind is None:  # some other Voter event with the wallet in topic1
                continue
            data = row["data"].removeprefix("0x")
            events[kind].append(
                {
                    "pool": Web3.to_checksum_address("0x" + row["topics"][2][-40:]),
                    "token_id": int(row["topics"][3], 16),
                    "weight": int(data[0:64], 16) / WEI,
                    "ts": int(data[128:192], 16),
                    "tx": row["transactionHash"],
                }
            )
    return events


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


def main() -> None:
    config = load_config()
    w3 = Web3(Web3.HTTPProvider(f"https://base-mainnet.g.alchemy.com/v2/{config.alchemy_api_key}"))
    http = httpx.Client(timeout=60)
    rewards_sugar = w3.eth.contract(
        address=Web3.to_checksum_address(REWARDS_SUGAR), abi=REWARDS_SUGAR_ABI
    )
    now = int(time.time())
    epoch_start = now // SECONDS_PER_WEEK * SECONDS_PER_WEEK

    # bound the log scan: votes can't predate the wallet's first ledger
    # activity; Base blocks tick every 2 seconds. Also load veNFT ownership
    # windows — the operator trades veNFTs, and a SOLD veNFT's last vote
    # otherwise "stands" in the reconstruction forever, inflating history
    conn = open_ledger(config.db_path)
    try:
        first_activity = conn.execute("SELECT MIN(ts) FROM events").fetchone()[0]
        nft_moves = conn.execute(
            """
            SELECT token_id, kind, ts FROM events
            WHERE lower(contract) = ? AND token_id IS NOT NULL ORDER BY ts
            """,
            (VOTING_ESCROW.lower(),),
        ).fetchall()
    finally:
        conn.close()
    first_activity = first_activity or now - 400 * 86400
    moves_by_token: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for token_id, kind, ts in nft_moves:
        moves_by_token[int(token_id)].append((ts, kind))

    def owned_at(token_id: int, at_ts: int) -> bool:
        owned = False
        for ts, kind in moves_by_token.get(token_id, ()):
            if ts > at_ts:
                break
            owned = kind == "transfer_in"
        return owned
    current_block = w3.eth.block_number
    from_block = max(0, current_block - (now - first_activity + 30 * 86400) // 2)
    print(f"scanning Voter logs from block {from_block} (~{day(first_activity)} minus margin)")

    # -- 1. every vote action, per veNFT, chronological ------------------------
    # a tx with Voted events sets that veNFT's votes; a tx with only
    # Abstained events clears them (reset/withdraw); votes persist otherwise
    actions: dict[int, list[tuple[int, dict[str, float]]]] = defaultdict(list)
    for _label, wallet in config.addresses.items():
        wallet_events = fetch_vote_events(http, wallet, from_block, current_block)
        voted, abstained = wallet_events["voted"], wallet_events["abstained"]
        by_tx: dict[str, dict] = {}
        for event in voted:
            entry = by_tx.setdefault(
                event["tx"], {"ts": event["ts"], "votes": defaultdict(dict)}
            )
            entry["votes"][event["token_id"]][event["pool"]] = event["weight"]
        for event in abstained:  # reset-only txs clear the veNFT's standing votes
            by_tx.setdefault(event["tx"], {"ts": event["ts"], "votes": defaultdict(dict)})
            by_tx[event["tx"]]["votes"].setdefault(event["token_id"], {})
        for entry in by_tx.values():
            for token_id, pools in entry["votes"].items():
                actions[token_id].append((entry["ts"], pools))
    for token_id in actions:
        actions[token_id].sort(key=lambda a: a[0])
    if not actions:
        raise SystemExit("no Voted events found for the tracked wallets")
    first_vote_ts = min(a[0][0] for a in actions.values())
    print(f"vote history: {sum(len(a) for a in actions.values())} vote actions across "
          f"{len(actions)} veNFTs since {day(first_vote_ts)}")

    # -- 2. reward history for every pool ever voted ---------------------------
    voted_pools = sorted({pool for acts in actions.values() for _, pools in acts for pool in pools})
    span_epochs = (epoch_start - first_vote_ts) // SECONDS_PER_WEEK + HISTORY_EPOCHS + 2
    pool_epochs: dict[str, dict[int, dict]] = {}
    pool_names: dict[str, str] = {}
    for lp in voted_pools:
        pool = w3.eth.contract(address=lp, abi=POOL_ABI)
        try:
            symbols = []
            for fn in ("token0", "token1"):
                token = getattr(pool.functions, fn)().call()
                erc20 = w3.eth.contract(address=token, abi=ERC20_ABI)
                symbols.append(erc20.functions.symbol().call())
            pool_names[lp] = f"{_pool_kind(pool)}-{'/'.join(symbols)}"
        except Exception:  # noqa: BLE001 — odd pool; the address still identifies it
            pool_names[lp] = lp[:10]
        rows = rewards_sugar.functions.epochsByAddress(int(span_epochs), 0, lp).call()
        pool_epochs[lp] = {
            ts: {"votes": votes / WEI, "legs": [*bribes, *fees]}
            for ts, _lp, votes, _em, bribes, fees in rows
            if ts < epoch_start
        }

    # -- 3. value rewards at claim-day prices, then walk-forward targets -------
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

    by_claim_ts: dict[int, set[str]] = defaultdict(set)
    for epochs in pool_epochs.values():
        for ts, e in epochs.items():
            by_claim_ts[ts + SECONDS_PER_WEEK].update(t.lower() for t, _ in e["legs"])
    prices_at = {
        ts: fetch_historical_prices(http, min(ts, now), tokens)
        for ts, tokens in sorted(by_claim_ts.items())
    }
    for epochs in pool_epochs.values():
        for ts, e in sorted(epochs.items()):
            prices = prices_at[ts + SECONDS_PER_WEEK]
            e["reward_usd"] = sum(
                amount / 10 ** decimals_of(token) * prices.get(token.lower(), 0.0)
                for token, amount in e["legs"]
            )
            e["usd_per_1k"] = e["reward_usd"] / e["votes"] * 1000 if e["votes"] else 0.0
        ordered = sorted(epochs.items())
        for index, (_ts, e) in enumerate(ordered):
            prior = [x for _, x in ordered[max(0, index - HISTORY_EPOCHS) : index]]
            e["target_per_1k"] = (
                statistics.median(x["usd_per_1k"] for x in prior) if len(prior) >= 3 else None
            )

    # -- 4. the report card -----------------------------------------------------
    def standing_votes(token_id: int, at_ts: int) -> dict[str, float]:
        state: dict[str, float] = {}
        for ts, pools in actions[token_id]:
            if ts >= at_ts:
                break
            state = pools
        return state

    print(f"\n{'epoch':<12} {'pool':<26} {'my votes':>9} {'target$':>8} {'real$':>8} "
          f"{'real/1k':>8} {'tgt/1k':>7}  verdict")
    epochs_all = range(
        first_vote_ts // SECONDS_PER_WEEK * SECONDS_PER_WEEK, epoch_start, SECONDS_PER_WEEK
    )
    unknown_ownership = {t for t in actions if t not in moves_by_token}
    if unknown_ownership:
        print(f"warning: no ledger ownership record for veNFTs {sorted(unknown_ownership)}; "
              f"their votes are excluded")
    total_target, total_real, hits, misses, judged = 0.0, 0.0, 0, 0, 0
    for ts in epochs_all:
        epoch_end = ts + SECONDS_PER_WEEK
        merged: dict[str, float] = defaultdict(float)
        for token_id in actions:
            # rewards go to votes standing at the close — but only veNFTs
            # actually OWNED then; sold/merged-away NFTs vote for their new owner
            if not owned_at(token_id, epoch_end - 1):
                continue
            for pool, weight in standing_votes(token_id, epoch_end).items():
                merged[pool] += weight
        if merged:
            print(f"{day(ts):<12} -- standing power {sum(merged.values()) / 1000:.1f}k "
                  f"across {len(merged)} pools --")
        for pool, weight in sorted(merged.items(), key=lambda kv: -kv[1]):
            e = pool_epochs.get(pool, {}).get(ts)
            if e is None or not e["votes"]:
                print(f"{day(ts):<12} {pool_names.get(pool, pool[:10]):<26} "
                      f"{weight / 1000:>8.1f}k {'?':>8} {'?':>8} {'?':>8} {'?':>7}  no epoch data")
                continue
            real = e["reward_usd"] * weight / e["votes"]
            target = e["target_per_1k"] * weight / 1000 if e["target_per_1k"] else None
            total_real += real
            verdict = "n/a (young pool)"
            if target is not None:
                judged += 1
                total_target += target
                hit = real >= target * HIT_THRESHOLD
                hits, misses = hits + hit, misses + (not hit)
                verdict = "HIT" if hit else "miss"
            target_txt = f"{target:.2f}" if target else "—"
            target_1k_txt = f"{e['target_per_1k']:.2f}" if e["target_per_1k"] else "—"
            print(f"{day(ts):<12} {pool_names.get(pool, pool[:10]):<26} "
                  f"{weight / 1000:>8.1f}k "
                  f"{target_txt:>8} {real:>8.2f} "
                  f"{e['usd_per_1k']:>8.2f} {target_1k_txt:>7}"
                  f"  {verdict}")

    print(f"\nverdict over {judged} judged pool-epochs "
          f"(target = scout's walk-forward $/1k at vote time, hit = ≥{HIT_THRESHOLD:.0%}):")
    print(f"  hits {hits} / misses {misses}"
          + (f"  ({hits / (hits + misses):.0%} hit rate)" if hits + misses else ""))
    print(f"  total targeted ${total_target:,.2f} vs realized ${total_real:,.2f}"
          + (f"  ({total_real / total_target:.0%} of target)" if total_target else ""))
    print("\ncaveats: realized = your weight-share of the epoch's final rewards at")
    print("claim-day prices — what the vote EARNED, independent of when you claimed;")
    print("pools younger than 3 epochs at vote time have no target and aren't judged")


if __name__ == "__main__":
    main()
