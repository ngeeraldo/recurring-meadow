import math

from scripts import config


def test_outgoing_probabilities_per_from_state_sum_at_most_one():
    assert sum(config.FROM_ACTIVE.values()) <= 1.0
    assert sum(config.FROM_PAST_DUE.values()) <= 1.0
    assert sum(config.FROM_CANCELED.values()) <= 1.0


def test_initial_tier_weights_sum_to_one():
    assert math.isclose(sum(config.INITIAL_TIER_WEIGHTS.values()), 1.0)


def test_initial_cadence_weights_sum_to_one():
    assert math.isclose(sum(config.INITIAL_CADENCE_WEIGHTS.values()), 1.0)


def test_tier_ladder_starts_at_standard_and_ends_at_enterprise():
    assert config.TIER_LADDER[0] == "standard"
    assert config.TIER_LADDER[-1] == "enterprise"
    assert "free" not in config.TIER_LADDER


def test_initial_tier_weights_keys_match_ladder():
    assert set(config.INITIAL_TIER_WEIGHTS.keys()) == set(config.TIER_LADDER)


def test_acquisition_expected_value_matches_target():
    expected_per_day = (
        config.ACQUISITION_ROLLS_PER_DAY * config.ACQUISITION_P_PER_ROLL
    )
    assert math.isclose(expected_per_day * 30, config.NEW_CUSTOMERS_PER_MONTH_AVG)
