from tools.analyze_sndk_rh_phase3 import (
    evaluate_candidate_horizons,
    replay_candidates,
)


def _quote(venue, received_ns, bid, ask):
    return {
        "record_type": "quote",
        "venue": venue,
        "received_wall_ns": received_ns,
        "bids": [[bid, 1.0]],
        "asks": [[ask, 1.0]],
        "parameter_valid": True,
        "parameter_invalid_reason": None,
        "midline_bps": 0.0,
        "upper_bps": 1.0,
        "lower_bps": 1.0,
        "effective_at": 1_700_000_000,
        "window_start": 1_699_971_200,
        "window_end": 1_700_000_000,
        "sample_count": 480,
        "coverage": 1.0,
    }


def test_counterfactual_skew_replay_changes_candidate_events():
    rows = [
        _quote("entropy", 0, 101.0, 101.0),
        _quote("lighter-rh", 120_000_000, 100.0, 100.0),
        _quote("lighter-rh", 200_000_000, 100.0, 102.0),
        _quote("lighter-rh", 500_000_000, 100.0, 100.0),
    ]

    counts = {}
    for skew_ms in (100.0, 250.0, 500.0):
        candidates, _ = replay_candidates(
            rows, skew_threshold_ms=skew_ms, warmup_ms=0.0
        )
        counts[skew_ms] = len(candidates)

    assert counts == {100.0: 0, 250.0: 1, 500.0: 2}


def test_horizon_evaluation_is_forward_only_and_marks_right_censoring():
    rows = [
        _quote("entropy", 0, 101.0, 101.0),
        _quote("lighter-rh", 1_000_000, 100.0, 100.0),
        _quote("entropy", 101_000_000, 101.0, 101.0),
    ]
    candidates, observations = replay_candidates(
        rows, skew_threshold_ms=250.0, warmup_ms=0.0
    )

    evaluated = evaluate_candidate_horizons(
        candidates,
        observations,
        horizons_ms=(100, 250),
        tolerance_ms=50.0,
    )
    by_horizon = {row["target_ms"]: row for row in evaluated}

    assert by_horizon[100]["status"] == "observed"
    assert by_horizon[100]["observed_wall_ns"] >= 101_000_000
    assert by_horizon[250]["status"] == "right_censored"
    assert by_horizon[250]["observed_wall_ns"] is None
