import pytest

from entropy_arb.premium import calculate_premiums


def test_calculate_premiums_matches_recorder_definitions():
    values = calculate_premiums(
        entropy_bid=100.10,
        entropy_ask=100.20,
        hedge_bid=99.90,
        hedge_ask=100.00,
    )
    entropy_mid = (100.10 + 100.20) / 2
    hedge_mid = (99.90 + 100.00) / 2
    assert values.premium_bps == pytest.approx((entropy_mid / hedge_mid - 1) * 1e4)
    assert values.sell_edge_bps == pytest.approx((100.10 / 100.00 - 1) * 1e4)
    assert values.buy_edge_bps == pytest.approx((99.90 / 100.20 - 1) * 1e4)


def test_calculate_premiums_is_directionally_consistent():
    values = calculate_premiums(101.0, 101.1, 100.0, 100.1)
    assert values.premium_bps > 0
    assert values.sell_edge_bps > 0
    assert values.buy_edge_bps < 0
