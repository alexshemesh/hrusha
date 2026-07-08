"""Unit tests for invest_scout scoring and risk tagging (no network)."""

from hrusha.service.invest_scout import (
    IdleBalance,
    InvestResult,
    RawOpportunity,
    aggregate_balances,
    rank,
    render_table,
    scan,
    tag_risks,
)


def _raw(
    protocol="aave-v3",
    mechanism="lending",
    token="USDC",  # noqa: S107  crypto symbol, not password
    apy=4.0,
    avail=10_000_000.0,
    util=0.5,
) -> RawOpportunity:
    return RawOpportunity(
        protocol=protocol,
        mechanism=mechanism,
        token=token,
        supply_apy_pct=apy,
        available_liquidity_usd=avail,
        utilization=util,
    )


# -- tag_risks ----------------------------------------------------------------


def test_every_opportunity_has_sc_risk():
    tags, _, _ = tag_risks(_raw(), balance_usd=1000)
    assert "sc-risk" in tags


def test_seamless_tagged_base_native():
    tags, _, notes = tag_risks(_raw(protocol="seamless"), balance_usd=1000)
    assert "base-native" in tags
    assert any("Base-native" in n for n in notes)


def test_lido_tagged_withdrawal_slow():
    raw = _raw(protocol="lido", mechanism="staking", token="ETH", avail=None)
    tags, safe, _ = tag_risks(raw, balance_usd=1000)
    assert "withdrawal-slow" in tags
    assert "beacon-queue" in tags
    assert safe is False


def test_high_utilization_flagged():
    raw = _raw(util=0.90)
    tags, safe, notes = tag_risks(raw, balance_usd=1000)
    assert "utilization-high" in tags
    assert safe is False
    assert any("utilization" in n for n in notes)


def test_low_liquidity_flagged():
    raw = _raw(avail=500.0)
    tags, safe, notes = tag_risks(raw, balance_usd=1000)
    assert "liquidity-low" in tags
    assert safe is False


def test_safe_lending_not_flagged():
    raw = _raw(util=0.50, avail=10_000_000.0)
    tags, safe, _ = tag_risks(raw, balance_usd=1000)
    assert safe is True
    assert "liquidity-low" not in tags
    assert "utilization-high" not in tags
    assert "withdrawal-slow" not in tags


def test_utilization_not_checked_tagged_when_none():
    raw = _raw(util=None)
    tags, safe, notes = tag_risks(raw, balance_usd=1000)
    assert "utilization-not-checked" in tags
    # Not blocking — still safe (just unverified)
    assert safe is True
    assert any("utilization not verified" in n for n in notes)


def test_aerodrome_depeg_and_il_tags():
    raw = _raw(protocol="aerodrome-slipstream", mechanism="stable-lp")
    tags, _, _ = tag_risks(raw, balance_usd=1000)
    assert "depeg-risk" in tags
    assert "il-low" in tags


# -- rank ---------------------------------------------------------------------


def test_safe_options_rank_above_unsafe():
    raws = [
        _raw(protocol="lido", mechanism="staking", token="ETH", apy=10.0, avail=None),
        _raw(protocol="aave-v3", mechanism="lending", token="ETH", apy=2.0),
    ]
    balances = [IdleBalance(token="ETH", amount=1.0, usd_value=3000)]
    scored = rank(raws, balances)
    # Aave (safe, 2%) ranks above Lido (unsafe, 10%)
    assert scored[0].withdrawal_safe is True
    assert scored[0].raw.protocol == "aave-v3"
    assert scored[1].withdrawal_safe is False
    assert scored[1].raw.protocol == "lido"


def test_higher_apy_wins_within_safe_group():
    raws = [
        _raw(protocol="aave-v3", apy=2.0),
        _raw(protocol="seamless", apy=5.0),
    ]
    balances = [IdleBalance(token="USDC", amount=1000, usd_value=1000)]
    scored = rank(raws, balances)
    assert scored[0].raw.protocol == "seamless"
    assert scored[1].raw.protocol == "aave-v3"


def test_unsafe_options_still_shown():
    raws = [
        _raw(protocol="lido", mechanism="staking", token="ETH", apy=10.0, avail=None),
        _raw(protocol="aave-v3", mechanism="lending", token="ETH", apy=2.0),
    ]
    balances = [IdleBalance(token="ETH", amount=1.0, usd_value=3000)]
    scored = rank(raws, balances)
    # Both are shown — unsafe is not filtered out
    assert len(scored) == 2


# -- aggregate_balances -------------------------------------------------------


def test_aggregate_balances_sums_per_token():
    from decimal import Decimal

    class FakeBalance:
        def __init__(self, token, amount, usd_value):
            self.token = token
            self.amount = Decimal(str(amount))
            self.usd_value = Decimal(str(usd_value))

    per_wallet = [
        FakeBalance("ETH", 1.0, 3000),
        FakeBalance("ETH", 0.5, 1500),
        FakeBalance("USDC", 500, 500),
        FakeBalance("DAI", 100, 100),  # ignored — not in scope
    ]
    result = aggregate_balances(per_wallet)
    by_token = {b.token: b for b in result}
    assert by_token["ETH"].amount == 1.5
    assert by_token["ETH"].usd_value == 4500
    assert by_token["USDC"].amount == 500
    assert "DAI" not in by_token


def test_aggregate_balances_skips_zero():
    from decimal import Decimal

    class FakeBalance:
        def __init__(self, token, amount, usd_value):
            self.token = token
            self.amount = Decimal(str(amount))
            self.usd_value = Decimal(str(usd_value))

    per_wallet = [FakeBalance("ETH", 0, 0)]
    result = aggregate_balances(per_wallet)
    assert result == []


# -- scan ---------------------------------------------------------------------


class FakeScanner:
    def __init__(self, opps):
        self._opps = opps

    def fetch_opportunities(self):
        return self._opps


def test_scan_returns_ranked_result():
    raws = [_raw(apy=4.0), _raw(protocol="seamless", apy=5.0)]
    balances = [IdleBalance(token="USDC", amount=1000, usd_value=1000)]
    result = scan(balances, FakeScanner(raws))
    assert isinstance(result, InvestResult)
    assert len(result.opportunities) == 2
    assert result.opportunities[0].raw.supply_apy_pct == 5.0  # ranked by APY


# -- render_table -------------------------------------------------------------


def test_render_table_shows_all_opportunities():
    raws = [
        _raw(apy=4.0),
        _raw(protocol="lido", mechanism="staking", token="ETH", apy=10.0, avail=None),
    ]
    balances = [
        IdleBalance(token="USDC", amount=1000, usd_value=1000),
        IdleBalance(token="ETH", amount=1.0, usd_value=3000),
    ]
    result = scan(balances, FakeScanner(raws))
    output = render_table(result)
    # Both opportunities appear — unsafe is not hidden
    assert "aave-v3" in output
    assert "lido" in output
    assert "withdrawal-slow" in output  # risk tag visible
    assert "sc-risk" in output


def test_render_table_shows_no_safe_option_when_all_unsafe():
    raws = [
        _raw(protocol="lido", mechanism="staking", token="ETH", apy=10.0, avail=None),
    ]
    balances = [IdleBalance(token="ETH", amount=1.0, usd_value=3000)]
    result = scan(balances, FakeScanner(raws))
    output = render_table(result)
    assert "no safe option found" in output


def test_render_table_shows_pool_url_when_present():
    raws = [_raw(apy=4.0, avail=10_000_000.0)]
    raws[0] = RawOpportunity(
        protocol="aave-v3",
        mechanism="lending",
        token="USDC",
        supply_apy_pct=4.0,
        available_liquidity_usd=10_000_000.0,
        utilization=0.5,
        pool_url="https://defillama.com/yields/pool/abc-123",
    )
    result = scan([IdleBalance(token="USDC", amount=100, usd_value=100)], FakeScanner(raws))
    output = render_table(result)
    assert "https://defillama.com/yields/pool/abc-123" in output


def test_40acres_tagged_self_repaying_and_not_safe():
    raw = RawOpportunity(
        protocol="40acres",
        mechanism="self-repaying-vault",
        token="USDC",
        supply_apy_pct=0.0,
        available_liquidity_usd=None,
        utilization=None,
        pool_url="https://app.40acres.finance",
    )
    score = tag_risks(raw, balance_usd=100.0)
    risk_tags, withdrawal_safe, notes = score
    assert "self-repaying" in risk_tags
    assert "liquidity-not-checked" in risk_tags
    assert withdrawal_safe is False  # self-repaying is a slow-exit tag
    assert raw.pool_url == "https://app.40acres.finance"


def test_reward_only_tag_when_apy_is_all_rewards():
    raw = RawOpportunity(
        protocol="aerodrome-slipstream",
        mechanism="stable-lp",
        token="USDC",
        supply_apy_pct=49.0,
        available_liquidity_usd=1_000_000.0,
        apy_base_pct=0.0,
        apy_reward_pct=49.0,
    )
    risk_tags, _, notes = tag_risks(raw, balance_usd=100.0)
    assert "reward-only" in risk_tags


def test_apy_declining_tag_when_prediction_is_down():
    raw = RawOpportunity(
        protocol="aerodrome-slipstream",
        mechanism="stable-lp",
        token="USDC",
        supply_apy_pct=30.0,
        available_liquidity_usd=1_000_000.0,
        prediction_class="Down",
        prediction_confidence=3,
    )
    risk_tags, _, _ = tag_risks(raw, balance_usd=100.0)
    assert "apy-declining" in risk_tags


def test_apy_volatile_tag_when_spot_is_2x_30d_avg():
    raw = RawOpportunity(
        protocol="aerodrome-slipstream",
        mechanism="stable-lp",
        token="USDC",
        supply_apy_pct=50.0,
        available_liquidity_usd=1_000_000.0,
        apy_30d_pct=15.0,
    )
    risk_tags, _, _ = tag_risks(raw, balance_usd=100.0)
    assert "apy-volatile" in risk_tags


def test_rank_uses_30d_avg_not_spot_apy():
    # pool A: high spot APY but low 30d avg (transient spike)
    # pool B: lower spot APY but higher 30d avg (sustained)
    raws = [
        RawOpportunity(
            protocol="a",
            mechanism="lending",
            token="USDC",
            supply_apy_pct=50.0,
            available_liquidity_usd=10_000_000.0,
            apy_30d_pct=5.0,
        ),
        RawOpportunity(
            protocol="b",
            mechanism="lending",
            token="USDC",
            supply_apy_pct=10.0,
            available_liquidity_usd=10_000_000.0,
            apy_30d_pct=8.0,
        ),
    ]
    result = scan([IdleBalance(token="USDC", amount=100, usd_value=100)], FakeScanner(raws))
    # pool B should rank first (higher 30d avg = 8% vs 5%)
    assert result.opportunities[0].raw.protocol == "b"
