"""Aerodrome vote scout: dilution-aware voting-pool suggestions.

Promoted from docs/examples/vote_scout_probe.py (see
docs/design-logs/2026-07-07-1126-vote-scout-probe.md for the research).

The displayed vAPR on the voting site is rewards-so-far / votes-so-far
and keeps moving until the epoch closes — a tiny pool showing a huge
number collapses when late voters pile in. This module ranks pools by
$ per vote against PROJECTED final votes (median of the pool's recent
completed epochs, where final votes are known), and attaches risk
flags; a pool with any flag never reaches the suggested list.

Data sources: RewardsSugar epochs (votes/incentives/fees per pool),
pool contracts directly for TVL (token0/token1 balances — deliberately
not LpSugar, whose unverified deployment has a version-drifting struct
layout), DefiLlama spot prices. Scoring is pure (`score_pool`) and
separated from chain I/O (`scan`) so the math is testable offline.

A full scan is ~200 chunked eth_calls (~3 minutes on the free Alchemy
tier) — callers run it on a background thread, never inline.
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx

from hrusha.adapters.known_contracts import (
    AERO_CONTRACT,
    AERODROME_FACTORY_REGISTRY,
    LEGACY_SLIPSTREAM_POOL_FACTORIES,
    REWARDS_SUGAR,
    VE_SUGAR,
)
from hrusha.config import Config, ScoutFilters
from hrusha.ledger.chain_cache import ChainCache, PoolMeta
from hrusha.ledger.store import open_ledger

log = logging.getLogger("hrusha.vote_scout")

BLOCKSCOUT_ABI_URL = "https://base.blockscout.com/api/v2/smart-contracts/{address}"
DEFILLAMA_PRICES_URL = "https://coins.llama.fi/prices/current/{coins}"
DEFILLAMA_FIRST_URL = "https://coins.llama.fi/prices/first/{coins}"
SECONDS_PER_WEEK = 604_800
SECONDS_PER_YEAR = 31_536_000
WEI = 10**18

# epochsLatest(_limit, _offset) scans a POOL-INDEX window over the
# concatenation of every registry factory's pool list and drops dead
# gauges — a short page does NOT mean the end of the list
POOL_INDEXES_PER_CALL = 200  # on-chain MAX_EPOCHS=200 caps returned rows
TOP_CANDIDATES = 100  # pools that get the expensive per-pool deep look
HISTORY_EPOCHS = 6  # completed epochs used to project final votes
MAX_SCAN_WORKERS = 4  # parallel RPC workers for paging + per-candidate deep look
PRICE_BATCH = 50  # DefiLlama coins per request (URL length bound)
HTTP_TIMEOUT_SECONDS = 30.0

# risk-gate defaults live in config.ScoutFilters — operator-tunable via the
# vote_scout section of ~/.hrusha/config.yaml (docs/examples/pool_filter_lab.py
# re-derives them from realized epoch data)
MIN_PRICE_CONFIDENCE = 0.8  # DefiLlama confidence below this = sketchy pricing
STRESS_VOTE_FACTOR = 1.15  # stress case: historical max final votes, plus this

# bribe-quality gates (constants, not config: these are boolean judgments,
# not thresholds the operator would tune per docs/examples/pool_filter_lab.py)
MIN_INCENTIVE_EPOCHS = 2  # a real bribe program shows up in the recent history...
INCENTIVE_MATTERS_SHARE = 0.3  # ...but only worth flagging when bribes drive the reward
SELF_BRIBE_MAX_SHARE = 0.5  # most incentive USD paid in the pool's own exotic token

# GoPlus token security (docs/examples/goplus_probe.py): free, keyless,
# covers Base. Queried ONE TOKEN PER CALL — the batch endpoint silently
# drops results (observed live). Only mechanical risks gate; mintable,
# proxy, holder concentration all trip on legit majors (AERO, USDC)
GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/8453"
GOPLUS_HARD_RISKS = (
    "is_honeypot",
    "cannot_sell_all",
    "transfer_pausable",
    "is_blacklisted",
    "owner_change_balance",
    "selfdestruct",
    "trading_cooldown",
    "hidden_owner",
)
GOPLUS_MAX_TAX = 0.05  # buy/sell tax above 5% is a toll booth

# symbol-level "established pair" preference (NOT a security boundary —
# symbols are spoofable; the TVL floor and DefiLlama-priced gates are what
# keep fakes out, this only expresses the operator's taste for majors)
MAJOR_SYMBOLS = frozenset(
    {
        "USDC",
        "USDbC",
        "USDT",
        "USDT0",
        "DAI",
        "USDS",
        "sUSDS",
        "USDe",
        "sUSDe",
        "GHO",
        "USD+",
        "EURC",
        "msUSD",
        "WETH",
        "ETH",
        "wstETH",
        "cbETH",
        "weETH",
        "rETH",
        "ezETH",
        "wrsETH",
        "msETH",
        "superOETHb",
        "cbBTC",
        "WBTC",
        "tBTC",
        "LBTC",
        "AERO",
    }
)

# hand-derived from contracts/RewardsSugar.vy (struct LpEpoch/LpEpochReward);
# the deployment is unverified, so scan() cross-checks the decode live: the
# running epoch's ts must equal the Thursday-anchored epoch start
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
    {
        "name": "poolFactories",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address[]"}],
    },
]
FACTORY_ABI = [
    {
        "name": "allPoolsLength",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]
POOL_ABI = [
    {
        "name": "token0",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "token1",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "tickSpacing",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "int24"}],
    },  # CL (Slipstream) pools only
    {
        "name": "stable",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bool"}],
    },  # v2 AMM pools only
]
ERC20_ABI = [
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "name": "decimals",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


@dataclass
class RawPool:
    """One pool's facts before scoring — chain-derived, judgment-free."""

    lp: str
    name: str  # e.g. "CL100-WETH/USDC"; chain-derived, treat as untrusted text
    symbols: tuple[str, ...]
    votes: float  # running epoch, veAERO units, still growing
    fees_usd: float
    incentives_usd: float
    blind_share: float  # 0..1 share of reward legs with no trustworthy price
    tvl_usd: float
    final_votes: tuple[float, ...] = ()  # completed epochs, newest first
    # days since DefiLlama first priced the YOUNGEST pair token; None = unknown
    # (never priced — treated as age 0 when the age gate is enabled)
    min_token_age_days: float | None = None
    # AERO emitted to the gauge over the SAME accrual window as fees_usd
    # (epoch start -> now), so fees/emissions compares like with like
    emissions_usd: float = 0.0
    incentive_epochs: int = 0  # completed history epochs that had any bribes
    # share of current incentive USD paid in the pool's own non-major tokens
    self_bribe_share: float = 0.0
    token_risks: tuple[str, ...] = ()  # GoPlus hard risks, e.g. "REI:sell_tax=8%"
    migrating: bool = False  # legacy Slipstream pool hidden from default Aerodrome vote views


@dataclass
class PoolScore:
    raw: RawPool
    projected_votes: float
    usd_per_1k: float
    stress_usd_per_1k: float
    vapr_pct: float
    fee_share: float
    flags: tuple[str, ...]  # blocking: any flag keeps the pool out of suggested
    my_usd_per_epoch: float
    my_epoch_pct: float
    my_apr_pct: float
    # informational (operator call 2026-07-07): shown in the table, never
    # block — EMISSIONS-SUBSIDIZED and SELF-BRIBED are "know what you're
    # being paid in", not "do not touch"; unproven by back-test as blockers
    notes: tuple[str, ...] = ()

    @property
    def suggested(self) -> bool:
        return not self.flags

    @property
    def reward_usd(self) -> float:
        return self.raw.fees_usd + self.raw.incentives_usd


@dataclass
class VeNft:
    id: int
    power: float
    voted_this_epoch: bool
    wallet_label: str
    active_pool_count: int = 0


@dataclass
class ScoutResult:
    scanned_at: int
    epoch_start: int
    cutoff_ts: int  # voting disabled in the epoch's final hour
    aero_price: float
    my_power: float
    my_value_usd: float
    venfts: list[VeNft] = field(default_factory=list)
    pools: list[PoolScore] = field(default_factory=list)  # ranked, best first
    # False when the GoPlus check was enabled but unreachable — the page
    # must say so; silently unchecked would read as "all tokens clean"
    token_safety_checked: bool = True

    @property
    def suggested(self) -> list[PoolScore]:
        return [p for p in self.pools if p.suggested]


# -- scoring: pure, offline-testable ------------------------------------------


def score_pool(
    raw: RawPool, aero_price: float, my_power: float, filters: ScoutFilters | None = None
) -> PoolScore:
    """Dilution-aware value of a vote for this pool, plus risk flags.

    Current votes are NOT final votes — late voters pile into juicy
    pools — so $ per vote is computed against max(votes so far, median
    of recent completed epochs). Any flag disqualifies the pool from
    the suggested list; the table still shows it with the flag visible.
    Gates come from the operator's ScoutFilters (config vote_scout
    section); UNPRICED-REWARDS is data integrity, never tunable.
    """
    filters = filters or ScoutFilters()
    reward_usd = raw.fees_usd + raw.incentives_usd
    projected = max(raw.votes, statistics.median(raw.final_votes)) if raw.final_votes else raw.votes
    stress = (
        max([raw.votes, *raw.final_votes]) * STRESS_VOTE_FACTOR
        if raw.final_votes
        else raw.votes * 2
    )
    usd_per_1k = reward_usd / projected * 1000 if projected else 0.0
    fee_share = raw.fees_usd / reward_usd if reward_usd else 0.0
    cv = (
        statistics.pstdev(raw.final_votes) / statistics.mean(raw.final_votes)
        if len(raw.final_votes) >= 2 and statistics.mean(raw.final_votes) > 0
        else None
    )

    flags = []
    if raw.migrating:
        flags.append("MIGRATING")
    if raw.tvl_usd < filters.min_tvl_usd:
        flags.append(f"LOW-TVL(${raw.tvl_usd:,.0f})")
    if len(raw.final_votes) < filters.min_history:
        flags.append(f"NEW({len(raw.final_votes)}ep)")
    if cv is not None and cv > filters.max_vote_cv:
        flags.append(f"VOLATILE-VOTES(cv={cv:.2f})")
    if raw.blind_share > 0:
        flags.append(f"UNPRICED-REWARDS({raw.blind_share:.0%})")
    if fee_share < filters.min_fee_share:
        flags.append(f"INCENTIVE-ONLY(fees={fee_share:.0%})")
    majors = MAJOR_SYMBOLS | set(filters.extra_major_symbols)
    if filters.require_major_pair and not set(raw.symbols) <= majors:
        flags.append("EXOTIC-PAIR")
    if filters.min_token_age_days > 0:
        age = raw.min_token_age_days or 0.0
        if age < filters.min_token_age_days:
            flags.append(f"YOUNG-TOKEN({age:.0f}d)")
    incentives_share = raw.incentives_usd / reward_usd if reward_usd else 0.0
    if (
        incentives_share > INCENTIVE_MATTERS_SHARE
        and raw.final_votes
        and raw.incentive_epochs < MIN_INCENTIVE_EPOCHS
    ):
        # bribes drive this reward but barely existed before: pump, not program
        flags.append(f"ONE-OFF-BRIBE({raw.incentive_epochs}/{len(raw.final_votes)}ep)")
    if raw.token_risks:
        flags.append(f"TOKEN-RISK({', '.join(raw.token_risks[:3])})")

    notes = []  # informational: shown, never blocks the suggested list
    if filters.min_fees_per_emission > 0 and raw.emissions_usd > 0:
        efficiency = raw.fees_usd / raw.emissions_usd
        if efficiency < filters.min_fees_per_emission:
            notes.append(f"EMISSIONS-SUBSIDIZED(fees/emit={efficiency:.2f})")
    if raw.self_bribe_share > SELF_BRIBE_MAX_SHARE:
        notes.append(f"SELF-BRIBED({raw.self_bribe_share:.0%})")

    my_usd = reward_usd * my_power / (projected + my_power) if projected + my_power else 0.0
    my_value_usd = my_power * aero_price
    my_epoch_pct = my_usd / my_value_usd * 100 if my_value_usd else 0.0
    return PoolScore(
        raw=raw,
        projected_votes=projected,
        usd_per_1k=usd_per_1k,
        stress_usd_per_1k=reward_usd / stress * 1000 if stress else 0.0,
        vapr_pct=(
            usd_per_1k / (1000 * aero_price) * (SECONDS_PER_YEAR / SECONDS_PER_WEEK) * 100
            if aero_price
            else 0.0
        ),
        fee_share=fee_share,
        flags=tuple(flags),
        my_usd_per_epoch=my_usd,
        my_epoch_pct=my_epoch_pct,
        my_apr_pct=my_epoch_pct * SECONDS_PER_YEAR / SECONDS_PER_WEEK,
        notes=tuple(notes),
    )


def rank(
    raws: list[RawPool],
    aero_price: float,
    my_power: float,
    filters: ScoutFilters | None = None,
) -> list[PoolScore]:
    scored = [score_pool(raw, aero_price, my_power, filters) for raw in raws]
    scored.sort(key=lambda p: -p.my_apr_pct)
    return scored


# -- chain scan: the slow part, run on a background thread --------------------


def _factory_pages(factories: list[tuple[str, int]], page_size: int = POOL_INDEXES_PER_CALL):
    """Yield (factory, limit, global offset) without crossing factory boundaries."""
    global_offset = 0
    for factory, pool_count in factories:
        for local_offset in range(0, pool_count, page_size):
            yield factory, min(page_size, pool_count - local_offset), global_offset + local_offset
        global_offset += pool_count


def _fetch_page(rewards_sugar, limit: int, offset: int):
    """Fetch one epochsLatest page — pure RPC, thread-safe (independent request)."""
    return rewards_sugar.functions.epochsLatest(limit, offset).call()


def _describe_token(w3, token_meta: dict, web3_cls, address: str) -> tuple[str, int]:
    """symbol + decimals for an ERC-20, cached in token_meta (idempotent writes)."""
    if address not in token_meta:
        try:
            erc20 = w3.eth.contract(address=web3_cls.to_checksum_address(address), abi=ERC20_ABI)
            token_meta[address] = (
                erc20.functions.symbol().call(),
                erc20.functions.decimals().call(),
            )
        except Exception:  # noqa: BLE001
            token_meta[address] = (address[:10], 18)
    return token_meta[address]


@dataclass(frozen=True)
class _FreshFacts:
    """Chain facts a candidate actually fetched, for the caller to persist.

    Workers never touch SQLite (one connection, one thread), so what they
    learn comes back here and the caller writes it after the fan-out joins.
    """

    pool_meta: tuple[str, str, str] | None = None  # (token0, token1, kind)
    epochs: tuple[tuple[int, float, bool], ...] | None = None  # completed, newest first


def _scan_candidate(
    p: dict,
    w3,
    web3_cls,
    rewards_sugar,
    http: httpx.Client,
    prices: dict,
    token_meta: dict,
    epoch_start: int,
    now: int,
    aero_price: float,
    token_decimals: dict,
    pool_meta: PoolMeta | None = None,
    cached_epochs: list[tuple[int, float, bool]] | None = None,
) -> tuple[RawPool, tuple[str, str] | None, _FreshFacts]:
    """Deep-look one candidate pool: token0/token1, TVL, vote history.

    Pure RPC/HTTP — no SQLite. Shared caches (prices, token_meta) are mutated
    via idempotent atomic dict ops under the GIL; duplicate RPCs on a cache
    miss race are harmless (same result). `pool_meta` and `cached_epochs` are
    plain values read from the ledger by the CALLER before fan-out (a sqlite3
    connection belongs to one thread), and stay None when uncached.
    Returns (RawPool, pair_tokens|None).
    """
    lp = web3_cls.to_checksum_address(p["lp"])
    pool = w3.eth.contract(address=lp, abi=POOL_ABI)
    if pool_meta is not None:
        token0, token1 = pool_meta.token0, pool_meta.token1
    else:
        try:
            token0, token1 = pool.functions.token0().call(), pool.functions.token1().call()
        except Exception:  # noqa: BLE001 — not a standard pool; keep it, visibly unpriced
            return (
                RawPool(
                    lp=p["lp"],
                    name=p["lp"][:10],
                    symbols=(),
                    votes=p["votes"],
                    fees_usd=p["fees_usd"],
                    incentives_usd=p["incentives_usd"],
                    blind_share=p["blind_share"],
                    tvl_usd=0.0,
                    migrating=p["migrating"],
                ),
                None,
                _FreshFacts(),
            )
    pair = (token0.lower(), token1.lower())
    needed = {token0.lower(), token1.lower()} - set(prices)
    if needed:
        prices.update(_fetch_prices(http, needed))  # GIL-atomic; idempotent
    tvl = 0.0
    symbols = []
    for token in (token0, token1):
        symbol, decimals = _describe_token(w3, token_meta, web3_cls, token.lower())
        symbols.append(symbol)
        erc20 = w3.eth.contract(address=web3_cls.to_checksum_address(token), abi=ERC20_ABI)
        balance = erc20.functions.balanceOf(lp).call() / 10**decimals
        tvl += balance * prices.get(token.lower(), (0.0, 0.0))[0]
    if cached_epochs is not None:
        completed, fresh_epochs = cached_epochs[:HISTORY_EPOCHS], None
    else:
        history = rewards_sugar.functions.epochsByAddress(HISTORY_EPOCHS + 1, 0, lp).call()
        # normalize to the cache's shape: (epoch_ts, final votes, had bribes)
        fresh_epochs = tuple(
            (int(ts), votes / WEI, bool(bribes))
            for ts, _lp, votes, _em, bribes, _fees in history
            if ts < epoch_start  # completed epochs only — the running one still moves
        )
        completed = list(fresh_epochs[:HISTORY_EPOCHS])
    incentive_usd_total, self_bribe_usd = 0.0, 0.0
    for token, amount in p["bribes"]:
        token = token.lower()  # noqa: PLW2901
        usd = amount / 10 ** token_decimals.get(token, 18) * prices.get(token, (0.0, 0.0))[0]
        incentive_usd_total += usd
        if token in pair:
            symbol = _describe_token(w3, token_meta, web3_cls, token)[0]
            if symbol not in MAJOR_SYMBOLS:
                self_bribe_usd += usd  # bribing voters with the pool's own exotic token
    kind = pool_meta.kind if pool_meta is not None else _pool_kind(pool)
    return (
        RawPool(
            lp=p["lp"],
            name=f"{kind}-{'/'.join(symbols)}",
            symbols=tuple(symbols),
            votes=p["votes"],
            fees_usd=p["fees_usd"],
            incentives_usd=p["incentives_usd"],
            blind_share=p["blind_share"],
            tvl_usd=tvl,
            final_votes=tuple(votes for _ts, votes, _had_bribes in completed),
            emissions_usd=p["emissions_rate"] * (now - epoch_start) * aero_price,
            incentive_epochs=sum(1 for _ts, _votes, had_bribes in completed if had_bribes),
            self_bribe_share=(self_bribe_usd / incentive_usd_total if incentive_usd_total else 0.0),
            migrating=p["migrating"],
        ),
        pair,
        _FreshFacts(
            pool_meta=None if pool_meta is not None else (token0.lower(), token1.lower(), kind),
            epochs=fresh_epochs,
        ),
    )


def scan(config: Config) -> ScoutResult:
    """Full scan of every alive gauge on Base; never call inline.

    Parallelized via ThreadPoolExecutor (MAX_SCAN_WORKERS): parallel
    epochsLatest paging + parallel per-candidate deep look. Fail-fast —
    a failed fetch propagates via .result() before results are consumed.

    Immutable chain facts come from (and go back to) the ledger's cache
    tables; the cache is an optimization, so a ledger that won't open
    degrades to the previous full-fetch behaviour instead of failing.
    """
    conn = _open_cache_conn(config)
    try:
        return _scan(config, ChainCache(conn) if conn is not None else None)
    finally:
        if conn is not None:
            conn.close()


def _open_cache_conn(config: Config) -> sqlite3.Connection | None:
    """The ledger connection backing the chain-fact caches, or None.

    The scout runs on a background thread in the dashboard; a locked or
    unreadable ledger must cost speed, never the scan.
    """
    try:
        return open_ledger(config.db_path)
    except Exception as exc:  # noqa: BLE001 — the cache is optional by design
        log.warning(
            "vote scout running without chain-fact cache", extra={"why": type(exc).__name__}
        )
        return None


def _scan(config: Config, cache: ChainCache | None) -> ScoutResult:
    """The scan proper, against an optional fact cache (None = fetch it all)."""
    from web3 import Web3  # deferred: read-only dashboard pages never need web3

    w3 = Web3(Web3.HTTPProvider(f"https://base-mainnet.g.alchemy.com/v2/{config.alchemy_api_key}"))
    http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
    now = int(time.time())
    epoch_start = now // SECONDS_PER_WEEK * SECONDS_PER_WEEK

    rewards_sugar = w3.eth.contract(
        address=Web3.to_checksum_address(REWARDS_SUGAR), abi=REWARDS_SUGAR_ABI
    )
    registry = w3.eth.contract(
        address=Web3.to_checksum_address(AERODROME_FACTORY_REGISTRY), abi=REGISTRY_ABI
    )
    factory_lengths = [
        (
            factory.lower(),
            w3.eth.contract(address=factory, abi=FACTORY_ABI).functions.allPoolsLength().call(),
        )
        for factory in registry.functions.poolFactories().call()
    ]
    pool_count = sum(length for _factory, length in factory_lengths)
    log.info("vote scout scanning", extra={"pool_indexes": pool_count})

    pools: list[dict] = []
    seen: set[str] = set()  # defensive: window semantics must never yield a pool twice
    pages = list(_factory_pages(factory_lengths))
    with ThreadPoolExecutor(max_workers=MAX_SCAN_WORKERS) as ex:
        page_futs = [
            ex.submit(_fetch_page, rewards_sugar, limit, offset)
            for _factory, limit, offset in pages
        ]
        page_rows = [f.result() for f in page_futs]  # fail-fast; parallel I/O
    for (factory, _limit, _offset), rows in zip(pages, page_rows, strict=True):
        for ts, lp, votes, emissions, bribes, fees in rows:
            if ts != epoch_start:  # decode sanity: running epoch must be Thursday-anchored
                raise RuntimeError(f"LpEpoch decode looks wrong: ts={ts} != {epoch_start}")
            if lp.lower() in seen:
                continue
            seen.add(lp.lower())
            pools.append(
                {
                    "lp": lp,
                    "votes": votes / WEI,
                    "bribes": bribes,
                    "fees": fees,
                    # AERO/second to the gauge; scaled to the fees accrual
                    # window below so fees/emissions compares like with like
                    "emissions_rate": emissions / WEI,
                    "migrating": factory in LEGACY_SLIPSTREAM_POOL_FACTORIES,
                }
            )

    reward_tokens = {token.lower() for p in pools for token, _ in [*p["bribes"], *p["fees"]]}
    reward_tokens.add(AERO_CONTRACT)
    prices = _fetch_prices(http, reward_tokens)
    aero_price = prices.get(AERO_CONTRACT, (0.0, 0.0))[0]

    # Warm the in-memory caches from the ledger, then hand plain dicts to the
    # workers: symbol/decimals and a pool's tokens never change, so a scan
    # that re-derives them from chain is paying for nothing. Reads happen
    # here, on one thread — sqlite3 objects never cross into the pool.
    token_meta: dict[str, tuple[str, int]] = cache.token_meta_all() if cache else {}
    warmed_tokens = set(token_meta)

    def describe(address: str) -> tuple[str, int]:
        return _describe_token(w3, token_meta, Web3, address.lower())

    token_decimals = {
        token.lower(): describe(token)[1]
        for p in pools
        for token, _ in [*p["bribes"], *p["fees"]]
        if token.lower() in prices
    }
    for p in pools:
        p["fees_usd"], fee_blind = _reward_usd(p["fees"], prices, token_decimals)
        p["incentives_usd"], bribe_blind = _reward_usd(p["bribes"], prices, token_decimals)
        p["blind_share"] = max(fee_blind, bribe_blind)
    # Rank candidates by APY proxy (reward $ / running votes) so high-yield
    # low-vote pools surface — not just pools with the most total reward $
    pools.sort(key=lambda p: -((p["fees_usd"] + p["incentives_usd"]) / max(p["votes"], 1)))
    candidates = [p for p in pools if p["fees_usd"] + p["incentives_usd"] > 0][:TOP_CANDIDATES]

    # A pool's history is immutable once its epochs close, but only through
    # the epoch the cache was last filled to: a marker older than the last
    # completed epoch means a week (or more) of history is missing.
    pool_facts = cache.pool_meta_many(p["lp"] for p in candidates) if cache else {}
    history_fresh_from = epoch_start - SECONDS_PER_WEEK

    raws: list[RawPool] = []
    pair_tokens: dict[str, tuple[str, str]] = {}  # lp -> (token0, token1) lowercased
    fresh_by_lp: dict[str, _FreshFacts] = {}
    with ThreadPoolExecutor(max_workers=MAX_SCAN_WORKERS) as ex:
        cand_futs = []
        for p in candidates:
            meta = pool_facts.get(p["lp"].lower())
            epochs = (
                cache.pool_epochs(p["lp"])
                if cache and meta is not None and meta.epochs_synced_ts >= history_fresh_from
                else None
            )
            cand_futs.append(
                ex.submit(
                    _scan_candidate,
                    p,
                    w3,
                    Web3,
                    rewards_sugar,
                    http,
                    prices,
                    token_meta,
                    epoch_start,
                    now,
                    aero_price,
                    token_decimals,
                    meta,
                    epochs,
                )
            )
        for i, fut in enumerate(cand_futs):
            raw, pair, fresh = fut.result()  # fail-fast; parallel RPC across candidates
            raws.append(raw)
            fresh_by_lp[candidates[i]["lp"]] = fresh
            if pair:
                pair_tokens[candidates[i]["lp"]] = pair

    _persist_pool_facts(cache, fresh_by_lp, epoch_start)
    if cache is not None:
        cache.store_token_meta_many(
            {token: meta for token, meta in token_meta.items() if token not in warmed_tokens}
        )

    # token age (days since DefiLlama's first price) for the YOUNG-TOKEN gate
    first_seen = _fetch_first_seen(http, {t for pair in pair_tokens.values() for t in pair}, cache)
    for raw in raws:
        pair = pair_tokens.get(raw.lp)
        if pair and all(t in first_seen for t in pair):
            raw.min_token_age_days = min((now - first_seen[t]) / 86400 for t in pair)

    token_safety_checked = True
    if config.vote_scout.token_safety:
        bribe_tokens = {t.lower() for p in candidates for t, _ in p["bribes"]}
        checked = {t for t in {*bribe_tokens, *(t for pr in pair_tokens.values() for t in pr)}
                   if token_meta.get(t, ("?",))[0] not in MAJOR_SYMBOLS}  # fmt: skip
        risks, token_safety_checked = _fetch_token_risks(
            http, sorted(checked), token_meta, cache, now
        )
        for p, raw in zip(candidates, raws, strict=False):
            exposed = {*pair_tokens.get(raw.lp, ()), *(t.lower() for t, _ in p["bribes"])}
            raw.token_risks = tuple(
                risk for token in sorted(exposed) for risk in risks.get(token, ())
            )

    venfts = _fetch_venfts(w3, http, config, epoch_start)
    my_power = sum(nft.power for nft in venfts)
    return ScoutResult(
        scanned_at=now,
        epoch_start=epoch_start,
        cutoff_ts=epoch_start + SECONDS_PER_WEEK - 3600,
        aero_price=aero_price,
        my_power=my_power,
        my_value_usd=my_power * aero_price,
        venfts=venfts,
        pools=rank(raws, aero_price, my_power, config.vote_scout),
        token_safety_checked=token_safety_checked,
    )


def _persist_pool_facts(
    cache: ChainCache | None, fresh_by_lp: dict[str, _FreshFacts], epoch_start: int
) -> None:
    """Write back what the workers learned. Metadata first: store_pool_epochs
    updates a pool_meta row, so the row has to exist before the marker does."""
    if cache is None:
        return
    for lp, fresh in fresh_by_lp.items():
        if fresh.pool_meta is not None:
            cache.store_pool_meta(lp, *fresh.pool_meta)
        if fresh.epochs is not None:
            # marker = the running epoch's start: every epoch that has
            # closed is now recorded, and next week's scan refetches
            cache.store_pool_epochs(lp, fresh.epochs, epoch_start)


def _fetch_token_risks(
    http: httpx.Client,
    tokens: list[str],
    token_meta: dict,
    cache: ChainCache | None = None,
    now: int | None = None,
) -> tuple[dict[str, tuple[str, ...]], bool]:
    """token -> GoPlus hard-risk strings; bool = the check actually ran.

    One token per call — GoPlus's batch endpoint silently drops results
    (docs/examples/goplus_probe.py). Positive findings only: a token
    GoPlus has never scanned is unknown, and unknown is not a risk flag
    (age/TVL/pricing gates carry that weight).

    Verdicts cache with a TTL (they are slow-changing, not immutable), and
    clean results cache too — otherwise the no-risk majority, which is most
    tokens, is refetched forever.
    """
    now = int(time.time()) if now is None else now
    risks: dict[str, tuple[str, ...]] = {}
    failures = 0
    fetched: list[str] = []
    for token in tokens:
        cached = cache.token_risks(token, now) if cache is not None else None
        if cached is not None:
            if cached:
                risks[token] = _with_symbol(token, cached, token_meta)
            continue
        fetched.append(token)
        try:
            response = http.get(GOPLUS_URL, params={"contract_addresses": token})
            response.raise_for_status()
            data = (response.json().get("result") or {}).get(token)
        except (httpx.HTTPError, ValueError):
            failures += 1
            continue
        # An unknown token (GoPlus never scanned it) caches as clean-with-TTL:
        # it is not a risk flag today, and re-asking every scan costs a call
        # per token to learn the same nothing.
        found = []
        if data:
            found = [field_name for field_name in GOPLUS_HARD_RISKS if data.get(field_name) == "1"]
            for side in ("buy_tax", "sell_tax"):
                raw_tax = data.get(side)
                if raw_tax not in (None, "") and float(raw_tax) > GOPLUS_MAX_TAX:
                    found.append(f"{side}={float(raw_tax):.0%}")
        if cache is not None:
            cache.store_token_risks(token, found, now)
        if found:
            risks[token] = _with_symbol(token, found, token_meta)
    if failures:
        log.warning("GoPlus token check failures", extra={"failed": failures, "of": len(fetched)})
    all_failed = bool(fetched) and failures == len(fetched)
    return risks, not all_failed


def _with_symbol(token: str, risks: Iterable[str], token_meta: dict) -> tuple[str, ...]:
    """Label risks with the token's symbol; the cache stores them raw so a
    later scan can re-label once the symbol is actually known."""
    symbol = token_meta.get(token, (token[:10],))[0]
    return tuple(f"{symbol}:{risk}" for risk in risks)


def _fetch_prices(http: httpx.Client, tokens: set[str]) -> dict[str, tuple[float, float]]:
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


def _fetch_first_seen(
    http: httpx.Client, tokens: set[str], cache: ChainCache | None = None
) -> dict[str, int]:
    """token -> unix ts of DefiLlama's FIRST recorded price (token age proxy).

    Hits are immutable and cached; misses are not. A token DefiLlama cannot
    price today may gain a price later, and caching that absence would pin
    the pool's age gate to "unknown" forever.
    """
    first: dict[str, int] = cache.first_seen(tokens) if cache is not None else {}
    todo = sorted(tokens - set(first))
    found: dict[str, int] = {}
    for start in range(0, len(todo), PRICE_BATCH):
        coins = ",".join(f"base:{t}" for t in todo[start : start + PRICE_BATCH])
        response = http.get(DEFILLAMA_FIRST_URL.format(coins=coins))
        response.raise_for_status()
        for coin, data in (response.json().get("coins") or {}).items():
            if data.get("timestamp"):
                found[coin.split(":", 1)[1].lower()] = int(data["timestamp"])
    if cache is not None:
        cache.store_first_seen(found)
    return first | found


def _reward_usd(legs, prices, token_decimals) -> tuple[float, float]:
    """(priced USD total, unpriced share 0..1) for a bribes/fees leg list."""
    total, blind = 0.0, 0.0
    for token, amount in legs:
        token = token.lower()
        price, confidence = prices.get(token, (0.0, 0.0))
        total += amount / 10 ** token_decimals.get(token, 18) * price
        if price <= 0.0 or confidence < MIN_PRICE_CONFIDENCE:
            blind += 1
    return total, (blind / len(legs) if legs else 0.0)


def _pool_kind(pool) -> str:
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


def _fetch_venfts(w3, http: httpx.Client, config: Config, epoch_start: int) -> list[VeNft]:
    """Lock sizes + voted-this-epoch per veNFT, via VeSugar's verified ABI."""
    from web3 import Web3

    response = http.get(BLOCKSCOUT_ABI_URL.format(address=VE_SUGAR))
    response.raise_for_status()
    abi = response.json().get("abi")
    if not abi:
        raise RuntimeError("no verified ABI on Blockscout for VeSugar")
    ve_sugar = w3.eth.contract(address=Web3.to_checksum_address(VE_SUGAR), abi=abi)
    fields = [
        c["name"]
        for c in ve_sugar.get_function_by_name("byAccount").abi["outputs"][0]["components"]
    ]
    venfts = []
    for label, wallet in config.addresses.items():
        for raw in ve_sugar.functions.byAccount(Web3.to_checksum_address(wallet)).call():
            nft = dict(zip(fields, raw, strict=True))
            venfts.append(
                VeNft(
                    id=nft["id"],
                    power=nft["voting_amount"] / WEI,
                    voted_this_epoch=nft["voted_at"] >= epoch_start,
                    wallet_label=label,
                    active_pool_count=len(nft["votes"]),
                )
            )
    return venfts
