"""DefiLlama yields scanner for Base investment opportunities.

Uses the free DefiLlama yields API (no key) to fetch APY + TVL for
lending, staking, and stable-LP pools on Base.  This is the same data
family hrusha already uses for historical prices.

The scanner produces ``RawOpportunity`` objects for the pure scorer in
``hrusha.service.invest_scout``.  Chain I/O only — no scoring logic here.

Withdrawal safety note: DefiLlama TVL is total supplied, not
available liquidity.  We do NOT set available_liquidity_usd for
lending pools (it would be a false claim).  Utilization is unknown
from DefiLlama, so ``utilization-not-checked`` is tagged and the
scorer blocks such entries from "best safe" until real on-chain
utilization data is wired.
"""

from __future__ import annotations

import httpx


class ScannerError(Exception):
    """Raised when the underlying data source is unreachable or returns invalid data."""


# Tokens we track
TARGET_TOKENS = {"USDC", "WETH", "ETH", "CBBTC", "CBTC", "CBETH", "WSTETH", "STETH"}

# Map DefiLlama symbols to our canonical token names
# cbETH is excluded — user wants ETH, cbBTC, USDC only
SYMBOL_MAP = {
    "WETH": "ETH",
    "CBBTC": "cbBTC",
    "WSTETH": "ETH",  # wrapped stETH — still ETH exposure
    "STETH": "ETH",
}

# Projects we consider low-risk enough to surface
LENDING_PROJECTS = {"aave-v3", "seamless", "morpho-blue"}
STAKING_PROJECTS = {"lido"}
# Aerodrome stable pairs: USDC paired with another stable
STABLE_LP_KEYWORDS = ("USDC-USDC", "MSUSD-USDC", "USDC-AERO")

YIELDS_URL = "https://yields.llama.fi/pools"


def _classify(project: str, symbol: str) -> str | None:
    """Classify a pool into a mechanism, or None if not low-risk."""
    project_l = project.lower()
    if project_l in LENDING_PROJECTS:
        return "lending"
    if project_l in STAKING_PROJECTS:
        return "staking"
    # Aerodrome stable LPs only — skip volatile pairs (WETH-USDC etc.)
    if "aerodrome" in project_l:
        sym_u = symbol.upper()
        # Only pure stablecoin pairs: both sides are USD-pegged
        stable_tokens = {"USDC", "USDT", "DAI", "USDBC", "MSUSD", "PYUSD"}
        parts = sym_u.split("-")
        if len(parts) == 2 and all(p in stable_tokens for p in parts):
            return "stable-lp"
    return None


def _map_token(symbol: str) -> str | None:
    """Map a DefiLlama symbol to our canonical token, or None if irrelevant.

    Lending vaults (Morpho, Gauntlet, Steakhouse) issue share tokens with
    composite symbols — ``MWETH``, ``GTWETHB``, ``STEAKUSDC`` — so we match
    by substring, not just exact symbol.  cbETH is excluded by request.
    """
    s = symbol.upper()
    if "CBETH" in s:
        return None  # excluded — user wants ETH, cbBTC, USDC only
    if "USDC" in s:
        return "USDC"
    if "CBBTC" in s:
        return "cbBTC"
    if "WSTETH" in s or "STETH" in s or "WETH" in s:
        return "ETH"
    if s == "ETH":
        return "ETH"
    return None


class DefiLlamaInvestScanner:
    """Real scanner: fetches lending/staking/stable-LP pools from DefiLlama.

    Produces ``RawOpportunity`` objects for the pure scorer.
    """

    def __init__(self, timeout: int = 15):
        self._timeout = timeout

    def fetch_opportunities(self) -> list:
        from hrusha.service.invest_scout import RawOpportunity

        try:
            resp = httpx.get(YIELDS_URL, timeout=self._timeout)
            resp.raise_for_status()
        except Exception as exc:
            raise ScannerError(f"DefiLlama yields API unreachable: {exc}") from exc

        try:
            data = resp.json().get("data", [])
        except Exception as exc:
            raise ScannerError(f"DefiLlama yields API returned invalid JSON: {exc}") from exc
        opps: list[RawOpportunity] = []

        for pool in data:
            if pool.get("chain") != "Base":
                continue
            project = pool.get("project", "")
            symbol = pool.get("symbol", "")
            apy = pool.get("apy")
            tvl = pool.get("tvlUsd")

            if not isinstance(apy, (int, float)) or apy is None or apy < 0.01:
                continue
            if not isinstance(tvl, (int, float)) or tvl <= 0:
                continue

            # Hard filters: drop dead/garbage pools that aren't real opportunities
            # outlier: DefiLlama's own anomaly flag (spurious APY spikes)
            if pool.get("outlier") is True:
                continue
            # TVL floor: too small to deploy meaningful capital
            if tvl < 50_000:
                continue

            mechanism = _classify(project, symbol)
            if mechanism is None:
                continue

            # Stable LP: token is USDC (the stable we track)
            if mechanism == "stable-lp":
                if "USDC" not in symbol.upper():
                    continue
                token = "USDC"
                # Dead LP: zero trading volume in 7d = no fee revenue, reward farming only
                vol_7d = pool.get("volumeUsd7d")
                if vol_7d is None or (isinstance(vol_7d, (int, float)) and vol_7d <= 0):
                    continue
            else:
                token = _map_token(symbol)
                if token is None:
                    continue

            # Staking: only ETH exposure (wstETH/stETH)
            if mechanism == "staking" and token != "ETH":
                continue

            quirk_notes: tuple[str, ...] = ()
            if "morpho" in project.lower():
                # Morpho vaults: surface the vault name as a quirk
                quirk_notes = (f"morpho vault: {symbol}",)

            pool_id = pool.get("pool")
            pool_url = (
                f"https://defillama.com/yields/pool/{pool_id}" if isinstance(pool_id, str) else None
            )

            # DefiLlama yield-quality metadata for filtering/tagging/ranking
            apy_30d = pool.get("apyMean30d")
            apy_30d_pct = float(apy_30d) if isinstance(apy_30d, (int, float)) else None
            vol_7d = pool.get("volumeUsd7d")
            volume_7d_usd = float(vol_7d) if isinstance(vol_7d, (int, float)) else None
            apy_base = pool.get("apyBase")
            apy_base_pct = float(apy_base) if isinstance(apy_base, (int, float)) else None
            apy_reward = pool.get("apyReward")
            apy_reward_pct = float(apy_reward) if isinstance(apy_reward, (int, float)) else None
            is_outlier = pool.get("outlier") is True
            predictions = pool.get("predictions") or {}
            if isinstance(predictions, dict):
                prediction_class = predictions.get("predictedClass")
                prediction_confidence = predictions.get("binnedConfidence", 0)
            else:
                prediction_class = None
                prediction_confidence = 0

            opps.append(
                RawOpportunity(
                    protocol=project.lower(),
                    mechanism=mechanism,
                    token=token,
                    supply_apy_pct=float(apy),
                    available_liquidity_usd=None,  # unknown from DefiLlama (TVL ≠ withdrawable)
                    total_supply_usd=float(tvl),
                    utilization=None,  # not available from DefiLlama
                    quirk_notes=quirk_notes,
                    pool_url=pool_url,
                    apy_30d_pct=apy_30d_pct,
                    volume_7d_usd=volume_7d_usd,
                    apy_base_pct=apy_base_pct,
                    apy_reward_pct=apy_reward_pct,
                    is_outlier=is_outlier,
                    prediction_class=prediction_class,
                    prediction_confidence=int(prediction_confidence or 0),
                )
            )

        # 40acres AERO-USDC vault is not indexed by DefiLlama, so we surface
        # it as a known on-chain vault.  APY is unknown (share-price
        # appreciation from epochRewardsLocked drips — see FortyAcresAdapter);
        # we show 0% with a transparent note rather than hide it.
        from hrusha.adapters.known_contracts import FORTY_ACRES_VAULT

        opps.append(
            RawOpportunity(
                protocol="40acres",
                mechanism="self-repaying-vault",
                token="USDC",
                supply_apy_pct=0.0,
                available_liquidity_usd=None,  # not verified — not in DefiLlama
                total_supply_usd=None,
                utilization=None,
                quirk_notes=(
                    f"apy not derived — share-price appreciation vault ({FORTY_ACRES_VAULT[:8]}…)",
                    "liquidity not verified — vault not indexed by DefiLlama",
                ),
                pool_url="https://app.40acres.finance",
            )
        )

        return opps
