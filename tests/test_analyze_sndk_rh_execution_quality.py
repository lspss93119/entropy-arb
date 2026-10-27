from __future__ import annotations

import pytest

from tools.analyze_sndk_rh_execution_quality import (
    _event_row,
    _summarize_events,
    evaluate_book_pair,
    select_horizon_state,
    walk_to_notional,
)


def test_walk_to_notional_partially_consumes_last_level_without_overfill():
    result = walk_to_notional(
        [(100.0, 1.0), (101.0, 1.0)],
        150.0,
    )

    assert result["complete"] is True
    assert result["qty"] == pytest.approx(1.49504950495)
    assert result["notional_usd"] == pytest.approx(150.0)
    assert result["vwap"] == pytest.approx(100.3311258278)
    assert result["levels_consumed"] == 2


def test_walk_to_notional_marks_five_level_depth_shortfall():
    result = walk_to_notional(
        [(100.0, 1.0), (101.0, 1.0)],
        250.0,
    )

    assert result["complete"] is False
    assert result["reason"] == "depth_insufficient"
    assert result["notional_usd"] == pytest.approx(201.0)


def test_evaluate_book_pair_maps_sell_entropy_and_applies_leg_fee():
    state = {
        "event_wall_ns": 1_000_000_000,
        "pair_skew_ms": 10.0,
        "parameter_valid": True,
        "source_timestamp_status": {"entropy": "present", "lighter-rh": "present"},
        "entropy": {
            "bids": [[101.0, 1.0], [100.5, 1.0]],
            "asks": [[102.0, 1.0]],
        },
        "lighter-rh": {
            "bids": [[99.0, 1.0]],
            "asks": [[100.0, 1.0], [101.0, 1.0]],
        },
        "sell_edge_bps": 100.0,
        "buy_edge_bps": -294.1176470588,
    }
    fees = {
        "zero": {"buy_fee_bps": 0.0, "sell_fee_bps": 0.0},
        "entropy_raw": {"buy_fee_bps": 0.0, "sell_fee_bps": 4.5},
    }

    result = evaluate_book_pair(state, "sell_entropy", 150.0, fees)

    assert result["status"] == "complete"
    assert result["buy_venue"] == "lighter-rh"
    assert result["sell_venue"] == "entropy"
    assert result["bbo_edge_bps"] == pytest.approx(100.0)
    assert result["gross_edge_bps"] == pytest.approx(50.1650165)
    assert result["fee_results"]["zero"]["net_edge_bps"] == pytest.approx(50.1650165)
    assert result["fee_results"]["entropy_raw"]["net_edge_bps"] == pytest.approx(
        45.6424,
        rel=1e-3,
    )


def test_entropy_fee_scenario_follows_entropy_venue_not_buy_sell_side():
    state = {
        "event_wall_ns": 1_000_000_000,
        "pair_skew_ms": 10.0,
        "parameter_valid": True,
        "source_timestamp_status": {"entropy": "present", "lighter-rh": "present"},
        "entropy": {"bids": [[101.0, 2.0]], "asks": [[102.0, 2.0]]},
        "lighter-rh": {"bids": [[99.0, 2.0]], "asks": [[100.0, 2.0]]},
        "sell_edge_bps": 100.0,
        "buy_edge_bps": -294.117647,
    }
    result = evaluate_book_pair(
        state,
        "sell_entropy",
        100.0,
        {"entropy_raw": {"entropy_fee_bps": 4.5, "lighter_rh_fee_bps": 0.0}},
    )

    assert result["fee_results"]["entropy_raw"]["buy_fee_bps"] == pytest.approx(0.0)
    assert result["fee_results"]["entropy_raw"]["sell_fee_bps"] == pytest.approx(4.5)


def test_evaluate_book_pair_does_not_fill_missing_sell_depth():
    state = {
        "event_wall_ns": 1_000_000_000,
        "pair_skew_ms": 10.0,
        "parameter_valid": True,
        "source_timestamp_status": {"entropy": "present", "lighter-rh": "present"},
        "entropy": {"bids": [[101.0, 0.1]], "asks": [[102.0, 2.0]]},
        "lighter-rh": {"bids": [[99.0, 0.1]], "asks": [[100.0, 2.0]]},
        "sell_edge_bps": 100.0,
        "buy_edge_bps": -294.1176470588,
    }

    result = evaluate_book_pair(
        state, "sell_entropy", 150.0,
        {"zero": {"buy_fee_bps": 0.0, "sell_fee_bps": 0.0}},
    )

    assert result["status"] == "incomplete"
    assert result["reason"] == "sell_depth_insufficient"


def test_select_horizon_state_is_causal_and_marks_missing_horizon():
    states = [
        {"event_wall_ns": 1_000_000_000},
        {"event_wall_ns": 101_000_000_000},
    ]

    assert select_horizon_state(states, 1_000_000_000, 100_000)["event_wall_ns"] == 101_000_000_000
    assert select_horizon_state(states, 1_000_000_000, 200_000) is None


def test_event_and_summary_preserve_vwap_and_nested_fee_metrics():
    signal = {
        "signal_id": "s1",
        "direction": "sell_entropy",
        "signal_wall_ns": 1_000_000_000,
        "sell_edge_bps": 100.0,
        "buy_edge_bps": -100.0,
    }
    state = {
        "event_wall_ns": 1_000_000_000,
        "pair_skew_ms": 10.0,
        "parameter_valid": True,
        "source_timestamp_status": {"entropy": "present", "lighter-rh": "present"},
        "entropy": {"bids": [[101.0, 2.0]], "asks": [[102.0, 2.0]]},
        "lighter-rh": {"bids": [[99.0, 2.0]], "asks": [[100.0, 2.0]]},
        "sell_edge_bps": 100.0,
        "buy_edge_bps": -294.117647,
    }
    fees = {"zero": {"buy_fee_bps": 0.0, "sell_fee_bps": 0.0}}
    event = _event_row(signal, "run", 0, 100.0, state, fees)

    assert event["gross_vwap_edge_bps"] == pytest.approx(100.0)
    summary, _ = _summarize_events([event], fees)
    row = next(item for item in summary if item["direction"] == "all" and item["delay_ms"] == 0)
    assert row["net_edge_p50_bps"] == pytest.approx(100.0)
    assert row["positive_net_count"] == 1
    assert row["depth_eligible_count"] == 1
    assert row["depth_sufficient_pct_of_depth_eligible"] == pytest.approx(100.0)
