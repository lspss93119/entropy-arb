from __future__ import annotations

from tools.analyze_sndk_rh_run02_persistence import (
    build_pair_observations,
    evaluate_signal_persistence,
)


def _params():
    return {
        "parameter_valid": True,
        "parameter_invalid_reason": None,
        "effective_at": 1_700_000_000,
        "sample_count": 480,
        "coverage": 1.0,
        "midline_bps": 0.0,
        "upper_bps": 1.0,
        "lower_bps": 1.0,
    }


def _quote(venue, received_ns, bid, ask):
    row = {
        "record_type": "quote",
        "venue": venue,
        "received_wall_ns": received_ns,
        "bids": [[bid, 1.0]],
        "asks": [[ask, 1.0]],
    }
    row.update(_params())
    return row


def _signal():
    return {
        "signal_id": "signal-1",
        "direction": "sell_entropy",
        "signal_wall_ns": 10_000_000,
        "sell_edge_bps": 100.0,
        "threshold_bps": 1.0,
        **_params(),
    }


def test_persistence_distinguishes_failure_recovery_from_continuous_survival():
    quotes = [
        _quote("entropy", 0, 101.0, 101.1),
        _quote("lighter-rh", 10_000_000, 100.0, 100.0),
        _quote("entropy", 120_000_000, 100.005, 100.105),
        _quote("entropy", 220_000_000, 101.0, 101.1),
        _quote("entropy", 260_000_000, 101.0, 101.1),
    ]
    observations = build_pair_observations(quotes, skew_threshold_ms=250.0)

    result = evaluate_signal_persistence(
        _signal(), observations, wait_ms=250, skew_threshold_ms=250.0,
    )

    assert result["complete_observed"] is True
    assert result["continuous_survived_observed"] is False
    assert result["first_failure_kind"] == "edge_below_threshold"
    assert result["first_failure_after_ms"] == 110.0
    assert result["continuous_valid_duration_ms"] == 110.0
    assert result["recovered_after_failure"] is True
    assert result["qualifies_at_horizon"] is True


def test_persistence_marks_missing_future_observation_right_censored():
    quotes = [
        _quote("entropy", 0, 101.0, 101.1),
        _quote("lighter-rh", 10_000_000, 100.0, 100.0),
        _quote("entropy", 120_000_000, 101.0, 101.1),
    ]
    observations = build_pair_observations(quotes, skew_threshold_ms=250.0)

    result = evaluate_signal_persistence(
        _signal(), observations, wait_ms=500, skew_threshold_ms=250.0,
    )

    assert result["complete_observed"] is False
    assert result["status"] == "right_censored"
    assert result["right_censored"] is True
    assert result["first_failure_kind"] is None
    assert result["continuous_valid_duration_ms"] is None
    assert result["last_observation_after_ms"] == 110.0
