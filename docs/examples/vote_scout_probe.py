"""Vote scout spike: can on-chain data rank Aerodrome voting pools safely?

Read-only probe — no SQLite, no sync, no state. It must show, before an
epoch's vote cutoff, a ranked list of candidate pools with enough context
to judge both PROFIT and RISK:

  1. current-epoch rewards per pool (incentives + fees, in USD) from
     RewardsSugar.epochsLatest — the same numbers behind the site's vAPR
  2. dilution-aware projected $ per 1,000 votes: current votes are NOT
     final votes; late voters pile into juicy pools, so we project final
     votes from the pool's own recent epochs (median of completed epochs)
  3. risk flags: tiny TVL (a $1k pool paying big incentives is a trap —
     one late whale vote erases the yield), volatile vote history,
     rewards paid in unpriced/unknown tokens, exotic pair tokens,
     nearly-no-fees pools (pure incentive farming, revocable loyalty)

Run:  .venv/bin/python docs/examples/vote_scout_probe.py

Mechanics (aero.drome.eth.limo/docs, contracts SPECIFICATION.md):
epochs flip Thursday 00:00 UTC; epoch N's fees + incentives go to the
wallets whose votes are standing at N's close, so voting late loses
nothing and gains information. Voting is disabled in the epoch's final
hour — the practical cutoff is Wednesday 23:00 UTC.

RewardsSugar/pool ABIs are hand-derived from the sugar repo sources
(github.com/velodrome-finance/sugar, contracts/RewardsSugar.vy) because
the Base deployment is not source-verified on Blockscout. The LpEpoch
struct decode is validated live: the running epoch's `ts` must equal the
Thursday-anchored epoch start or we abort loudly.
"""

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime

import httpx
from web3 import Web3

# public contract addresses live in hrusha/adapters/ — the only path the
# gitleaks pre-commit hook allowlists for raw 0x addresses
from hrusha.adapters.known_contracts import (
    AERO_CONTRACT,
    AERODROME_FACTORY_REGISTRY,
    REWARDS_SUGAR,
    VE_SUGAR,
)
from hrusha.config import load_config

BLOCKSCOUT_ABI_URL = "https://base.blockscout.com/api/v2/smart-contracts/{address}"
DEFILLAMA_PRICES_URL = "https://coins.llama.fi/prices/current/{coins}"
SECONDS_PER_WEEK = 604_800
SECONDS_PER_YEAR = 31_536_000
WEI = 10**18

# epochsLatest(_limit, _offset) scans a POOL-INDEX window [offset, offset+limit)
# and drops pools with dead gauges — a short result does NOT mean the list is
# done; step by the window size until Voter.length() is exhausted
EPOCHS_PER_CALL = 200  # window size; on-chain MAX_EPOCHS=200 caps returned rows
TOP_CANDIDATES = 40  # pools that get the expensive per-pool deep look
HISTORY_EPOCHS = 6  # completed epochs used to project final votes
PRICE_BATCH = 50  # DefiLlama coins per request (URL length bound)

MIN_TVL_USD = 300_000  # below this, late voters/withdrawals can erase the edge
MIN_HISTORY = 3  # fewer completed epochs = no basis to project dilution
MAX_VOTE_CV = 0.6  # coefficient of variation of final votes; above = erratic
MIN_FEE_SHARE = 0.10  # rewards <10% fees = pure incentive farming
MIN_PRICE_CONFIDENCE = 0.8  # DefiLlama confidence below this = sketchy pricing

# symbol-level "established pair" preference (NOT a security boundary —
# symbols are spoofable; the TVL floor and DefiLlama-priced gates are what
# keep fakes out, this only expresses the operator's taste for majors)
MAJOR_SYMBOLS = frozenset(
    {
        "USDC", "USDbC", "USDT", "USDT0", "DAI", "USDS", "sUSDS", "USDe", "sUSDe",
        "GHO", "USD+", "EURC", "msUSD",
        "WETH", "ETH", "wstETH", "cbETH", "weETH", "rETH", "ezETH", "wrsETH",
        "msETH", "superOETHb",
        "cbBTC", "WBTC", "tBTC", "LBTC",
        "AERO",
    }
)

# hand-derived from contracts/RewardsSugar.vy (struct LpEpoch/LpEpochReward)
LP_EPOCH_COMPONENTS = [
    {"name": "ts", "type": "uint256"},
    {"name": "lp", "type": "address"},
    {"name": "votes", "type": "uint256"},
    {"name": "emissions", "type": "uint256"},
    {
        "name": "bribes",
        "type": "tuple[]",
        "components": [{"name": "token", "type": "address"}, {"name": "amount", "type": "uint256"}],
    },
    {
        "name": "fees",
        "type": "tuple[]",
        "components": [{"name": "token", "type": "address"}, {"name": "amount", "type": "uint256"}],
    },
]
REWARDS_SUGAR_ABI = [
    {
        "name": "epochsLatest",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "_limit", "type": "uint256"}, {"name": "_offset", "type": "uint256"}],
        "outputs": [{"name": "", "type": "tuple[]", "components": LP_EPOCH_COMPONENTS}],
    },
    {
        "name": "epochsByAddress",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "_limit", "type": "uint256"},
            {"name": "_offset", "type": "uint256"},
            {"name": "_address", "type": "address"},
        ],
        "outputs": [{"name": "", "type": "tuple[]", "components": LP_EPOCH_COMPONENTS}],
    },
]
REGISTRY_ABI = [
    {"name": "poolFactories", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address[]"}]},
]
FACTORY_ABI = [
    {"name": "allPoolsLength", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}]},
]
POOL_ABI = [
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"name": "tickSpacing", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "int24"}]},  # CL (Slipstream) pools only
    {"name": "stable", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "bool"}]},  # v2 AMM pools only
]


def pool_kind(pool) -> str:
    """Aerodrome-style prefix: CL<tickSpacing>, sAMM (stable) or vAMM."""
    for probe_call in (
        lambda: f"CL{pool.functions.tickSpacing().call()}",  # Slipstream pools
        lambda: "sAMM" if pool.functions.stable().call() else "vAMM",  # v2 AMM
    ):
        try:
            return probe_call()
        except Exception:  # noqa: BLE001, S112 — wrong pool flavor; try the next
            continue
    return "?"
ERC20_ABI = [
    {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "string"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]


def when(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")


def fetch_prices(http: httpx.Client, tokens: set[str]) -> dict[str, tuple[float, float]]:
    """token address (lowercase) -> (usd price, confidence) via DefiLlama."""
    prices: dict[str, tuple[float, float]] = {}
    todo = sorted(tokens)
    for start in range(0, len(todo), PRICE_BATCH):
        coins = ",".join(f"base:{t}" for t in todo[start : start + PRICE_BATCH])
        response = http.get(DEFILLAMA_PRICES_URL.format(coins=coins), params={"searchWidth": "4h"})
        response.raise_for_status()
        for coin, data in (response.json().get("coins") or {}).items():
            if data.get("price") is None:  # DefiLlama lists some coins priceless
                continue
            address = coin.split(":", 1)[1].lower()
            prices[address] = (float(data["price"]), float(data.get("confidence") or 1.0))
    return prices


def reward_usd(legs, prices, token_decimals) -> tuple[float, float]:
    """(priced USD total, unpriced share 0..1) for a bribes/fees leg list."""
    total, blind = 0.0, 0.0
    for token, amount in legs:
        token = token.lower()
        price, confidence = prices.get(token, (0.0, 0.0))
        value = amount / 10 ** token_decimals.get(token, 18) * price
        if price <= 0.0 or confidence < MIN_PRICE_CONFIDENCE:
            blind += 1
        total += value
    share_blind = blind / len(legs) if legs else 0.0
    return total, share_blind


def main() -> None:
    config = load_config()
    w3 = Web3(Web3.HTTPProvider(f"https://base-mainnet.g.alchemy.com/v2/{config.alchemy_api_key}"))
    http = httpx.Client(timeout=30)
    print(f"connected: chain_id={w3.eth.chain_id}, block={w3.eth.block_number}")

    now = int(time.time())
    epoch_start = now // SECONDS_PER_WEEK * SECONDS_PER_WEEK
    cutoff = epoch_start + SECONDS_PER_WEEK - 3600  # final hour: voting disabled
    print(f"epoch started {when(epoch_start)}; vote cutoff {when(cutoff)} "
          f"({(cutoff - now) / 3600:.1f}h from now)")

    rewards_sugar = w3.eth.contract(
        address=Web3.to_checksum_address(REWARDS_SUGAR), abi=REWARDS_SUGAR_ABI
    )

    # -- 1. every gauged pool's running epoch: votes so far + rewards so far --
    # Sugar paginates over the concatenated pool lists of every factory in
    # the FactoryRegistry (v2 AMM + Slipstream CL) — NOT over Voter.length()
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(AERODROME_FACTORY_REGISTRY), abi=REGISTRY_ABI
    )
    pool_count = 0
    for factory in registry.functions.poolFactories().call():
        length = w3.eth.contract(address=factory, abi=FACTORY_ABI).functions.allPoolsLength().call()
        print(f"  factory {factory}: {length} pools")
        pool_count += length
    print(f"registry factories track {pool_count} pool indexes; scanning for alive gauges...")
    pools: list[dict] = []
    seen: set[str] = set()  # defensive: window semantics must never yield a pool twice
    for offset in range(0, pool_count, EPOCHS_PER_CALL):
        rows = rewards_sugar.functions.epochsLatest(EPOCHS_PER_CALL, offset).call()
        for ts, lp, votes, _emissions, bribes, fees in rows:
            if ts != epoch_start:  # decode sanity: running epoch must be Thursday-anchored
                raise SystemExit(f"LpEpoch decode looks wrong: ts={ts} != {epoch_start}")
            if lp.lower() in seen:
                continue
            seen.add(lp.lower())
            pools.append({"lp": lp, "votes": votes / WEI, "bribes": bribes, "fees": fees})
        if offset // EPOCHS_PER_CALL % 10 == 9:
            print(f"  scanned {offset + EPOCHS_PER_CALL}/{pool_count} indexes, "
                  f"{len(pools)} alive so far")
    print(f"gauged pools with a running epoch: {len(pools)}")

    # -- 2. price every reward token once, in batches ------------------------
    reward_tokens = {
        token.lower() for p in pools for token, _ in [*p["bribes"], *p["fees"]]
    }
    reward_tokens.add(AERO_CONTRACT)
    prices = fetch_prices(http, reward_tokens)
    aero_price = prices.get(AERO_CONTRACT, (0.0, 0.0))[0]
    print(f"priced {len(prices)}/{len(reward_tokens)} reward tokens; AERO=${aero_price:.4f}")

    token_meta: dict[str, tuple[str, int]] = {}  # address -> (symbol, decimals)

    def describe(address: str) -> tuple[str, int]:
        address = address.lower()
        if address not in token_meta:
            erc20 = w3.eth.contract(address=Web3.to_checksum_address(address), abi=ERC20_ABI)
            try:
                token_meta[address] = (erc20.functions.symbol().call(),
                                       erc20.functions.decimals().call())
            except Exception:  # noqa: BLE001 — spam/odd tokens: show the address instead
                token_meta[address] = (address[:10], 18)
        return token_meta[address]

    token_decimals: dict[str, int] = {}
    for p in pools:
        for token, _ in [*p["bribes"], *p["fees"]]:
            token = token.lower()
            if token in prices and token not in token_decimals:
                token_decimals[token] = describe(token)[1]

    for p in pools:
        p["fees_usd"], fee_blind = reward_usd(p["fees"], prices, token_decimals)
        p["incentives_usd"], bribe_blind = reward_usd(p["bribes"], prices, token_decimals)
        p["reward_usd"] = p["fees_usd"] + p["incentives_usd"]
        p["blind_share"] = max(fee_blind, bribe_blind)
    pools.sort(key=lambda p: -p["reward_usd"])
    candidates = [p for p in pools if p["reward_usd"] > 0][:TOP_CANDIDATES]

    # -- 3. deep look per candidate: pair, TVL, vote history -----------------
    for p in candidates:
        lp = Web3.to_checksum_address(p["lp"])
        pool = w3.eth.contract(address=lp, abi=POOL_ABI)
        try:
            token0, token1 = pool.functions.token0().call(), pool.functions.token1().call()
        except Exception:  # noqa: BLE001 — not a standard pool; skip the deep look
            p["name"], p["tvl_usd"], p["history"] = p["lp"][:10], 0.0, []
            continue
        pair_prices = fetch_prices(http, {token0.lower(), token1.lower()} - set(prices))
        prices.update(pair_prices)
        tvl = 0.0
        symbols = []
        for token in (token0, token1):
            symbol, decimals = describe(token)
            symbols.append(symbol)
            erc20 = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
            balance = erc20.functions.balanceOf(lp).call() / 10**decimals
            tvl += balance * prices.get(token.lower(), (0.0, 0.0))[0]
        p["name"] = f"{pool_kind(pool)}-{'/'.join(symbols)}"
        p["symbols"] = symbols
        p["tvl_usd"] = tvl
        history = rewards_sugar.functions.epochsByAddress(HISTORY_EPOCHS + 1, 0, lp).call()
        p["history"] = [
            {"ts": ts, "votes": votes / WEI,
             "reward_usd": sum(reward_usd(legs, prices, token_decimals)[0]
                               for legs in (bribes, fees))}
            for ts, _lp, votes, _em, bribes, fees in history
            if ts < epoch_start  # completed epochs only: those votes are FINAL
        ][:HISTORY_EPOCHS]

    # -- 4. score: dilution-aware $ per vote + risk flags ---------------------
    for p in candidates:
        final_votes = [h["votes"] for h in p["history"]]
        projected = max(p["votes"], statistics.median(final_votes)) if final_votes else p["votes"]
        stress = max([p["votes"], *final_votes]) * 1.15 if final_votes else p["votes"] * 2
        p["projected_votes"] = projected
        p["usd_per_1k"] = p["reward_usd"] / projected * 1000 if projected else 0.0
        p["stress_usd_per_1k"] = p["reward_usd"] / stress * 1000 if stress else 0.0
        p["vapr"] = (
            p["usd_per_1k"] / (1000 * aero_price) * (SECONDS_PER_YEAR / SECONDS_PER_WEEK) * 100
            if aero_price
            else 0.0
        )
        fee_share = p["fees_usd"] / p["reward_usd"] if p["reward_usd"] else 0.0
        cv = (
            statistics.pstdev(final_votes) / statistics.mean(final_votes)
            if len(final_votes) >= 2 and statistics.mean(final_votes) > 0
            else None
        )
        flags = []
        if p["tvl_usd"] < MIN_TVL_USD:
            flags.append(f"LOW-TVL(${p['tvl_usd']:,.0f})")
        if len(final_votes) < MIN_HISTORY:
            flags.append(f"NEW({len(final_votes)}ep)")
        if cv is not None and cv > MAX_VOTE_CV:
            flags.append(f"VOLATILE-VOTES(cv={cv:.2f})")
        if p["blind_share"] > 0:
            flags.append(f"UNPRICED-REWARDS({p['blind_share']:.0%})")
        if fee_share < MIN_FEE_SHARE:
            flags.append(f"INCENTIVE-ONLY(fees={fee_share:.0%})")
        if not set(p.get("symbols", [])) <= MAJOR_SYMBOLS:
            flags.append("EXOTIC-PAIR")
        p["flags"] = flags

    ranked = sorted(candidates, key=lambda p: -p["usd_per_1k"])
    print(f"\n{'pool':<26} {'TVL$':>12} {'votes(M)':>9} {'proj(M)':>8} {'fees$':>9} "
          f"{'incent$':>9} {'$/1k':>7} {'strss':>6} {'vAPR%':>6}  flags")
    for p in ranked:
        print(
            f"{p['name']:<26} {p['tvl_usd']:>12,.0f} {p['votes'] / 1e6:>9.2f} "
            f"{p['projected_votes'] / 1e6:>8.2f} {p['fees_usd']:>9,.0f} "
            f"{p['incentives_usd']:>9,.0f} {p['usd_per_1k']:>7.2f} "
            f"{p['stress_usd_per_1k']:>6.2f} {p['vapr']:>6.1f}  {' '.join(p['flags']) or '-'}"
        )

    # -- 5. my voting power → personal projection on the clean shortlist -----
    ve_abi_response = http.get(BLOCKSCOUT_ABI_URL.format(address=VE_SUGAR))
    ve_abi_response.raise_for_status()
    ve_sugar = w3.eth.contract(
        address=Web3.to_checksum_address(VE_SUGAR), abi=ve_abi_response.json()["abi"]
    )
    fields = [
        c["name"]
        for c in ve_sugar.get_function_by_name("byAccount").abi["outputs"][0]["components"]
    ]
    my_power = 0.0
    for label, wallet in config.addresses.items():
        for raw in ve_sugar.functions.byAccount(Web3.to_checksum_address(wallet)).call():
            nft = dict(zip(fields, raw, strict=True))
            my_power += nft["voting_amount"] / WEI
            voted = nft["voted_at"] >= epoch_start
            print(f"\n{label} veNFT #{nft['id']}: power {nft['voting_amount'] / WEI:,.0f}, "
                  f"voted this epoch: {'YES' if voted else 'NO'}")

    my_value_usd = my_power * aero_price  # what the vote power is worth in AERO terms
    print(f"\nSUGGESTED (no risk flags except EXOTIC-PAIR tolerated on none), "
          f"with your {my_power:,.0f} votes (≈${my_value_usd:,.0f}) in the denominator:")
    suggested = [p for p in ranked if not p["flags"]][:5]
    for rank, p in enumerate(suggested, start=1):
        mine = p["reward_usd"] * my_power / (p["projected_votes"] + my_power)
        epoch_pct = mine / my_value_usd * 100 if my_value_usd else 0.0
        apr_pct = epoch_pct * SECONDS_PER_YEAR / SECONDS_PER_WEEK
        print(f"  {rank}. {p['name']:<26} {epoch_pct:.3f}%/epoch = {apr_pct:.1f}% APR "
              f"(${mine:,.2f}; pool pays ${p['reward_usd']:,.0f} to "
              f"{p['projected_votes'] / 1e6:.2f}M votes, TVL ${p['tvl_usd']:,.0f})"
              f"\n     pool address: {p['lp']}")
    if not suggested:
        print("  none passed every gate — inspect the flagged table above")


if __name__ == "__main__":
    main()
