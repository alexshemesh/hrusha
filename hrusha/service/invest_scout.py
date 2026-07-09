"""Investment scout: read-only, transparent suggestions for idle capital.

Suggests where to deploy idle ETH, cbBTC, USDC on Base into low-risk,
fast-withdrawable positions.  Every opportunity is shown — nothing is
hidden — with risk tags so the operator sees quirks and makes the call.

Pattern follows vote_scout.py: chain I/O lives in provider modules;
scoring/tagging is pure and offline-testable.

The ``Safe?`` column is informational, not a filter.  Even options with
blocking risk tags appear in the table — the operator asked for full
transparency, not silent exclusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# -- data shapes ---------------------------------------------------------------


@dataclass(frozen=True)
class RawOpportunity:
    """One investment opportunity before scoring — chain-derived, judgment-free."""

    protocol: str  # "aave-v3", "seamless", "morpho", "aerodrome", "lido"
    mechanism: str  # "lending", "lending-vault", "stable-lp", "staking"
    token: str  # "ETH", "cbBTC", "USDC"
    supply_apy_pct: float  # annualized supply rate, %
    available_liquidity_usd: float | None  # None = not applicable (e.g. staking)
    # total supplied into the pool/vault/reserve, USD
    total_supply_usd: float | None = None
    # utilization ratio 0..1 for lending; None if not applicable
    utilization: float | None = None
    # protocol-specific known facts, surfaced as quirks by the scorer
    quirk_notes: tuple[str, ...] = ()
    # link to the pool/vault page on the hosting service, if known
    pool_url: str | None = None
    # 30-day average APY (%) — more stable than spot APY; None if unavailable
    apy_30d_pct: float | None = None
    # 7-day trading volume (USD) — None if N/A (lending/staking have no volume)
    volume_7d_usd: float | None = None
    # organic yield from fees (%) vs incentive rewards (%); None if unknown
    apy_base_pct: float | None = None
    apy_reward_pct: float | None = None
    # DefiLlama's own outlier flag — anomalous/garbage pools
    is_outlier: bool = False
    # DefiLlama yield prediction: "Down" / "Stable/Up" and confidence bin 1-3
    prediction_class: str | None = None
    prediction_confidence: int = 0


@dataclass(frozen=True)
class OpportunityScore:
    """An opportunity after risk tagging."""

    raw: RawOpportunity
    risk_tags: tuple[str, ...]  # every risk/quirk, transparent
    withdrawal_safe: bool  # available liquidity >= your balance AND no slow tag
    # informational: "best safe APY for this token" etc.
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdleBalance:
    """Idle balance available to deploy, aggregated across all wallets per token."""

    token: str
    amount: float  # human units (2.5 ETH, 5000 USDC)
    usd_value: float


@dataclass
class InvestResult:
    scanned_at: int  # unix timestamp
    balances: list[IdleBalance] = field(default_factory=list)
    opportunities: list[OpportunityScore] = field(default_factory=list)  # ranked, best first
    # False if a data source was unreachable; the report must say so
    liquidity_checked: bool = True

    @property
    def safe(self) -> list[OpportunityScore]:
        return [o for o in self.opportunities if o.withdrawal_safe]

    @property
    def best_safe_by_token(self) -> dict[str, OpportunityScore]:
        """First (highest-APY) withdrawal-safe option per token."""
        seen: dict[str, OpportunityScore] = {}
        for o in self.opportunities:
            if o.withdrawal_safe and o.raw.token not in seen:
                seen[o.raw.token] = o
        return seen


# -- risk tagging: pure, offline-testable -------------------------------------


# Tags that indicate you CANNOT withdraw quickly
SLOW_WITHDRAWAL_TAGS = frozenset({"beacon-queue", "withdrawal-slow", "self-repaying"})

# Utilization above this flags a lending reserve: withdrawal may fail
HIGH_UTILIZATION_THRESHOLD = 0.85

# Available liquidity below this fraction of your balance flags risk
LOW_LIQUIDITY_FRACTION = 1.0  # available must be >= balance to be safe


def tag_risks(
    raw: RawOpportunity,
    balance_usd: float,
) -> tuple[tuple[str, ...], bool, tuple[str, ...]]:
    """Produce risk tags, withdrawal_safe flag, and informational notes.

    Returns (risk_tags, withdrawal_safe, notes).  Pure function —
    no network, no side effects.
    """
    tags: list[str] = []
    notes: list[str] = []

    # Every on-chain position has smart-contract risk
    tags.append("sc-risk")

    # Protocol-specific tags
    if "seamless" in raw.protocol:
        tags.append("base-native")
        notes.append("quirk:Base-native Aave fork, shorter audit history")
    if "morpho" in raw.protocol:
        tags.append("curated")
    if "aerodrome" in raw.protocol:
        tags.append("depeg-risk")
        tags.append("il-low")
        notes.append("quirk:stable LP still has depeg + withdrawal-as-LP risk")
    if "lido" in raw.protocol:
        tags.append("beacon-queue")
        tags.append("withdrawal-slow")
        notes.append("quirk:ETH staking withdrawals go through beacon queue (hours-days)")

    # 40acres vault: not indexed by DefiLlama, APY and liquidity unknown
    if raw.protocol == "40acres":
        tags.append("self-repaying")
        tags.append("liquidity-not-checked")

    # Yield quality flags (from DefiLlama metadata)
    # reward-only: 100% of APY is incentive tokens, zero organic fee yield
    if (
        raw.apy_base_pct is not None
        and raw.apy_base_pct == 0
        and raw.apy_reward_pct
        and raw.apy_reward_pct > 0
    ):
        tags.append("reward-only")
        notes.append("quirk:100% reward APY, zero organic yield")
    elif raw.apy_base_pct is None and raw.apy_reward_pct and raw.apy_reward_pct > 0:
        tags.append("reward-only")
        notes.append("quirk:APY is entirely incentive rewards")
    # apy-declining: DefiLlama predicts yield will drop (confidence >= 2)
    if raw.prediction_class == "Down" and raw.prediction_confidence >= 2:
        tags.append("apy-declining")
        notes.append(f"quirk:yield predicted to fall (confidence {raw.prediction_confidence}/3)")
    # apy-volatile: spot APY is 2x+ the 30-day average (transient spike)
    if (
        raw.apy_30d_pct is not None
        and raw.apy_30d_pct > 0
        and raw.supply_apy_pct > 2 * raw.apy_30d_pct
    ):
        tags.append("apy-volatile")
        notes.append(f"quirk:spot {raw.supply_apy_pct:.1f}% vs 30d avg {raw.apy_30d_pct:.1f}%")

    # Lending-specific: utilization and liquidity checks
    if raw.mechanism in ("lending", "lending-vault"):
        if raw.utilization is None:
            tags.append("utilization-not-checked")
            notes.append("quirk:on-chain utilization not verified — TVL used as proxy")
        elif raw.utilization > HIGH_UTILIZATION_THRESHOLD:
            tags.append("utilization-high")
            notes.append(f"quirk:utilization {raw.utilization:.0%} — withdrawal may fail")
        if raw.available_liquidity_usd is not None and balance_usd > 0:
            if raw.available_liquidity_usd < balance_usd:
                tags.append("liquidity-low")
                notes.append(
                    f"quirk:available ${raw.available_liquidity_usd:,.0f} "
                    f"< your ${balance_usd:,.0f}"
                )

    # Staking: always slow withdrawal regardless of "liquidity"
    if raw.mechanism == "staking":
        tags.append("withdrawal-slow")

    # Surface any protocol-supplied quirk notes as explicit tags
    for q in raw.quirk_notes:
        notes.append(f"quirk:{q}")

    # Determine withdrawal safety
    has_slow_tag = bool(SLOW_WITHDRAWAL_TAGS & set(tags))
    has_liquidity_problem = "liquidity-low" in tags
    has_utilization_problem = "utilization-high" in tags
    has_unverified_liquidity = "utilization-not-checked" in tags or "liquidity-not-checked" in tags
    withdrawal_safe = not (
        has_slow_tag or has_liquidity_problem or has_utilization_problem or has_unverified_liquidity
    )

    return tuple(tags), withdrawal_safe, tuple(notes)


def score_opportunity(
    raw: RawOpportunity,
    balance_usd: float,
) -> OpportunityScore:
    """Tag one opportunity with risks and determine withdrawal safety."""
    tags, safe, notes = tag_risks(raw, balance_usd)
    return OpportunityScore(
        raw=raw,
        risk_tags=tags,
        withdrawal_safe=safe,
        notes=notes,
    )


def rank(
    raws: list[RawOpportunity],
    balances: list[IdleBalance],
) -> list[OpportunityScore]:
    """Score and rank all opportunities, best safe APY first.

    Safe options rank above unsafe ones; within each group, higher APY
    wins.  Unsafe options are still shown — just lower in the list.
    """
    balance_by_token = {b.token: b.usd_value for b in balances}
    scored = [score_opportunity(raw, balance_by_token.get(raw.token, 0.0)) for raw in raws]
    scored.sort(
        key=lambda o: (
            not o.withdrawal_safe,
            -(o.raw.apy_30d_pct if o.raw.apy_30d_pct is not None else o.raw.supply_apy_pct),
        )
    )
    return scored


# -- balance aggregation -----------------------------------------------------


def aggregate_balances(
    per_wallet: list,  # list[hrusha.providers.interface.TokenBalance]
) -> list[IdleBalance]:
    """Sum balances across all wallets, per token symbol.

    Takes the existing ``TokenBalance`` from the provider interface and
    produces per-token totals for the invest scout to score against.
    """
    from decimal import Decimal

    totals: dict[str, dict[str, Decimal]] = {}
    for b in per_wallet:
        if b.usd_value is None:
            continue
        sym = b.token
        if sym not in ("ETH", "cbBTC", "USDC"):
            continue
        if sym not in totals:
            totals[sym] = {"amount": Decimal(0), "usd": Decimal(0)}
        totals[sym]["amount"] += b.amount
        totals[sym]["usd"] += b.usd_value
    return [
        IdleBalance(token=sym, amount=float(d["amount"]), usd_value=float(d["usd"]))
        for sym, d in totals.items()
        if d["amount"] > 0
    ]


# -- chain scan interface -----------------------------------------------------


class InvestScanner(Protocol):
    """Protocol for the chain-I/O side, so tests can inject fakes."""

    def fetch_opportunities(self) -> list[RawOpportunity]: ...


def scan(
    balances: list[IdleBalance],
    scanner: InvestScanner,
) -> InvestResult:
    """Full scan: fetch opportunities, score them, rank.

    ``scanner`` is injected so the pure scoring path is testable without
    network.  The CLI wires a real scanner; tests pass a fake.
    """
    import time

    liquidity_checked = True
    try:
        raws = scanner.fetch_opportunities()
    except Exception:
        # Provider unreachable — mark data as unverified, return empty result
        return InvestResult(
            scanned_at=int(time.time()),
            balances=balances,
            opportunities=[],
            liquidity_checked=False,
        )
    scored = rank(raws, balances)
    return InvestResult(
        scanned_at=int(time.time()),
        balances=balances,
        opportunities=scored,
        liquidity_checked=liquidity_checked,
    )


# -- rendering ----------------------------------------------------------------


def render_table(result: InvestResult) -> str:
    """Render the invest-suggestions report as a plain-text table.

    Every opportunity is shown, with all risk tags and notes visible.
    Nothing is hidden.
    """
    lines: list[str] = []
    lines.append(f"hrusha invest-suggestions  (scanned {result.scanned_at})")
    lines.append("")

    if not result.liquidity_checked:
        lines.append(
            "WARNING: on-chain liquidity check was unreachable — "
            "Safe? column is based on APY only, not verified liquidity."
        )
        lines.append("")

    if not result.balances:
        lines.append("No idle balances to deploy.")
        return "\n".join(lines)

    lines.append("Idle balances:")
    for b in result.balances:
        lines.append(f"  {b.token}: {b.amount:,.4f}  (${b.usd_value:,.2f})")
    lines.append("")

    if not result.opportunities:
        lines.append("No opportunities found.")
        return "\n".join(lines)

    # Table header
    lines.append(
        f"{'Protocol':<12} {'Mechanism':<14} {'Token':<6} "
        f"{'APY':>7} {'Avail Liq':>14} {'Safe?':>6} Risk tags"
    )
    lines.append("-" * 90)

    for o in result.opportunities:
        r = o.raw
        liq = (
            f"${r.available_liquidity_usd:,.0f}" if r.available_liquidity_usd is not None else "n/a"
        )
        safe = "yes" if o.withdrawal_safe else "no"
        tags_str = ", ".join(o.risk_tags) if o.risk_tags else ""
        lines.append(
            f"{r.protocol:<12} {r.mechanism:<14} {r.token:<6} "
            f"{r.supply_apy_pct:>6.2f}% {liq:>14} {safe:>6} {tags_str}"
        )
        if r.pool_url:
            lines.append(f"  link: {r.pool_url}")
        for note in o.notes:
            lines.append(f"  {note}")

    # Summary: best safe option per token
    lines.append("")
    lines.append("Best safe options per token:")
    best = result.best_safe_by_token
    for tok, o in best.items():
        lines.append(
            f"  {tok}: {o.raw.protocol} {o.raw.mechanism} @ {o.raw.supply_apy_pct:.2f}% APY"
        )

    missing = {b.token for b in result.balances} - set(best)
    for tok in sorted(missing):
        lines.append(f"  {tok}: no safe option found")

    return "\n".join(lines)
