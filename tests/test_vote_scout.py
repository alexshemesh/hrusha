"""Pure scoring math for the Aerodrome vote scout (no chain, no HTTP)."""

from hrusha.service.vote_scout import RawPool, ScoutResult, rank, score_pool

AERO_PRICE = 0.50
MY_POWER = 10_000.0  # veAERO -> $5,000 at AERO_PRICE


def clean_pool(**overrides) -> RawPool:
    """A pool that should pass every risk gate."""
    fields = dict(
        lp="0x" + "a" * 40,
        name="CL100-WETH/USDC",
        symbols=("WETH", "USDC"),
        votes=2_000_000.0,
        fees_usd=9_000.0,
        incentives_usd=500.0,
        blind_share=0.0,
        tvl_usd=5_000_000.0,
        final_votes=(4_000_000.0, 5_000_000.0, 4_500_000.0, 4_200_000.0),
    )
    fields.update(overrides)
    return RawPool(**fields)


def test_clean_pool_is_suggested_with_no_flags():
    score = score_pool(clean_pool(), AERO_PRICE, MY_POWER)
    assert score.flags == ()
    assert score.suggested


def test_projection_uses_history_median_not_flattering_current_votes():
    # 2M votes now vs a 4.35M median of completed epochs: the median wins,
    # because late voters WILL show up like they did every recent epoch
    score = score_pool(clean_pool(), AERO_PRICE, MY_POWER)
    assert score.projected_votes == 4_350_000.0
    assert score.usd_per_1k == (9_500.0 / 4_350_000.0) * 1000


def test_projection_keeps_current_votes_when_already_above_history():
    score = score_pool(clean_pool(votes=6_000_000.0), AERO_PRICE, MY_POWER)
    assert score.projected_votes == 6_000_000.0


def test_stress_scenario_uses_historical_max_plus_margin():
    score = score_pool(clean_pool(), AERO_PRICE, MY_POWER)
    assert score.stress_usd_per_1k == (9_500.0 / (5_000_000.0 * 1.15)) * 1000


def test_personal_projection_includes_own_dilution_and_percent_terms():
    score = score_pool(clean_pool(), AERO_PRICE, MY_POWER)
    expected_usd = 9_500.0 * MY_POWER / (4_350_000.0 + MY_POWER)
    assert abs(score.my_usd_per_epoch - expected_usd) < 1e-9
    assert abs(score.my_epoch_pct - expected_usd / 5_000.0 * 100) < 1e-9
    assert abs(score.my_apr_pct - score.my_epoch_pct * (31_536_000 / 604_800)) < 1e-9


def test_tiny_tvl_pool_is_flagged_and_never_suggested():
    # the $1k-pool-with-big-rewards trap: huge $/vote, but one late whale
    # vote erases the yield — must carry LOW-TVL no matter how juicy
    score = score_pool(clean_pool(tvl_usd=1_000.0), AERO_PRICE, MY_POWER)
    assert any(flag.startswith("LOW-TVL") for flag in score.flags)
    assert not score.suggested


def test_short_history_is_flagged_new():
    score = score_pool(clean_pool(final_votes=(4_000_000.0,)), AERO_PRICE, MY_POWER)
    assert any(flag.startswith("NEW") for flag in score.flags)


def test_erratic_vote_history_is_flagged_volatile():
    score = score_pool(
        clean_pool(final_votes=(100_000.0, 9_000_000.0, 200_000.0, 8_000_000.0)),
        AERO_PRICE,
        MY_POWER,
    )
    assert any(flag.startswith("VOLATILE-VOTES") for flag in score.flags)


def test_unpriced_reward_tokens_are_flagged():
    score = score_pool(clean_pool(blind_share=0.5), AERO_PRICE, MY_POWER)
    assert any(flag.startswith("UNPRICED-REWARDS") for flag in score.flags)


def test_pure_incentive_pool_is_flagged():
    score = score_pool(clean_pool(fees_usd=100.0, incentives_usd=20_000.0), AERO_PRICE, MY_POWER)
    assert any(flag.startswith("INCENTIVE-ONLY") for flag in score.flags)


def test_non_major_pair_is_flagged_exotic():
    score = score_pool(clean_pool(symbols=("WETH", "MEMECOIN")), AERO_PRICE, MY_POWER)
    assert "EXOTIC-PAIR" in score.flags


def test_rank_orders_by_dilution_adjusted_dollars_per_vote():
    rich = clean_pool(fees_usd=50_000.0)
    poor = clean_pool(lp="0x" + "b" * 40, fees_usd=1_000.0)
    ranked = rank([poor, rich], AERO_PRICE, MY_POWER)
    assert [p.raw.fees_usd for p in ranked] == [50_000.0, 1_000.0]


def test_result_suggested_filters_flagged_pools():
    result = ScoutResult(
        scanned_at=0,
        epoch_start=0,
        cutoff_ts=0,
        aero_price=AERO_PRICE,
        my_power=MY_POWER,
        my_value_usd=MY_POWER * AERO_PRICE,
        pools=rank(
            [clean_pool(), clean_pool(lp="0x" + "c" * 40, tvl_usd=5.0)], AERO_PRICE, MY_POWER
        ),
    )
    assert [p.raw.tvl_usd for p in result.suggested] == [5_000_000.0]


def test_zero_history_zero_votes_pool_does_not_divide_by_zero():
    score = score_pool(
        clean_pool(votes=0.0, final_votes=(), fees_usd=0.0, incentives_usd=0.0),
        AERO_PRICE,
        MY_POWER,
    )
    assert score.usd_per_1k == 0.0
    assert score.stress_usd_per_1k == 0.0


def test_custom_filters_relax_the_tvl_gate():
    from hrusha.config import ScoutFilters

    relaxed = ScoutFilters(min_tvl_usd=10_000.0)
    score = score_pool(clean_pool(tvl_usd=50_000.0), AERO_PRICE, MY_POWER, relaxed)
    assert not any(flag.startswith("LOW-TVL") for flag in score.flags)
    assert score.suggested


def test_majors_gate_can_be_disabled():
    from hrusha.config import ScoutFilters

    open_pairs = ScoutFilters(require_major_pair=False)
    score = score_pool(clean_pool(symbols=("WETH", "REI")), AERO_PRICE, MY_POWER, open_pairs)
    assert "EXOTIC-PAIR" not in score.flags
    assert score.suggested


def test_extra_major_symbols_extend_the_builtin_set():
    from hrusha.config import ScoutFilters

    trusted = ScoutFilters(extra_major_symbols=("REI",))
    score = score_pool(clean_pool(symbols=("WETH", "REI")), AERO_PRICE, MY_POWER, trusted)
    assert "EXOTIC-PAIR" not in score.flags
    # a symbol NOT in the extended set still flags
    other = score_pool(clean_pool(symbols=("WETH", "MEMECOIN")), AERO_PRICE, MY_POWER, trusted)
    assert "EXOTIC-PAIR" in other.flags


def test_unpriced_rewards_flag_is_not_tunable():
    from hrusha.config import ScoutFilters

    permissive = ScoutFilters(
        min_tvl_usd=0.0,
        require_major_pair=False,
        max_vote_cv=99.0,
        min_fee_share=0.0,
        min_history=0,
    )
    score = score_pool(clean_pool(blind_share=0.4), AERO_PRICE, MY_POWER, permissive)
    assert any(flag.startswith("UNPRICED-REWARDS") for flag in score.flags)
    assert not score.suggested


def test_young_token_gate_off_by_default():
    score = score_pool(clean_pool(min_token_age_days=3.0), AERO_PRICE, MY_POWER)
    assert not any(flag.startswith("YOUNG-TOKEN") for flag in score.flags)


def test_young_token_gate_flags_new_and_unknown_tokens():
    from hrusha.config import ScoutFilters

    gated = ScoutFilters(min_token_age_days=90.0)
    young = score_pool(clean_pool(min_token_age_days=12.0), AERO_PRICE, MY_POWER, gated)
    assert "YOUNG-TOKEN(12d)" in young.flags
    unknown = score_pool(clean_pool(min_token_age_days=None), AERO_PRICE, MY_POWER, gated)
    assert "YOUNG-TOKEN(0d)" in unknown.flags  # never priced = treat as brand new
    seasoned = score_pool(clean_pool(min_token_age_days=400.0), AERO_PRICE, MY_POWER, gated)
    assert not any(flag.startswith("YOUNG-TOKEN") for flag in seasoned.flags)
