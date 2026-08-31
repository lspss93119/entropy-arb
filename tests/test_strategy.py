import math

import pytest

from entropy_arb.strategy import DriftingBasisStrategy, StableBasisStrategy


def feed_seconds(strategy, start, seconds, value_fn=lambda i: float(i)):
    for i in range(seconds):
        strategy.update(start + i, value_fn(i))


def test_stable_basis_is_immediately_ready_and_fixed():
    s = StableBasisStrategy(center_bps=-1.0, upper_bps=3.0, lower_bps=3.5)
    before = s.state()
    assert before.ready is True
    assert before.center_bps == -1.0
    assert before.upper_bps == 3.0
    assert before.lower_bps == 3.5

    s.update(1000.0, 25.0)
    after = s.state()
    assert after == before


def test_drifting_not_ready_before_full_window():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    feed_seconds(s, 1000.0, 60, lambda i: 1.0)
    assert s.state().ready is False
    s.update(1060.0, 1.0)
    assert s.state().ready is True
    assert s.state().center_bps == pytest.approx(1.0)


def test_drifting_requires_90_percent_coverage():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    for i in range(0, 61, 2):
        s.update(1000.0 + i, 2.0)
    state = s.state()
    assert state.warmup_span_sec >= 60
    assert state.coverage_ratio < 0.90
    assert state.ready is False


def test_drifting_uses_timestamp_window_and_causal_median():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    for i in range(61):
        s.update(1000.0 + i, 1.0 if i < 60 else 9.0)
    assert s.state().ready is True
    first_center = s.state().center_bps
    s.update(1061.0, 9.0)
    assert s.state().center_bps >= first_center


def test_short_gap_does_not_reset_history():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    feed_seconds(s, 1000.0, 40, lambda i: 1.0)
    s.update(1060.0, 1.0)
    assert s.state().warmup_span_sec >= 60


def test_gap_over_30_seconds_resets_history():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    feed_seconds(s, 1000.0, 61, lambda i: 1.0)
    assert s.state().ready is True
    s.update(1091.1, 5.0)
    state = s.state()
    assert state.ready is False
    assert state.warmup_span_sec == pytest.approx(0.0)
    assert state.center_bps is None


def test_nonfinite_premium_is_ignored():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    s.update(1000.0, 1.0)
    before = s.state()
    s.update(1001.0, math.nan)
    s.update(1002.0, math.inf)
    assert s.state() == before


def test_new_drifting_instance_never_restores_old_state():
    old = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    feed_seconds(old, 1000.0, 61, lambda i: 2.0)
    assert old.state().ready is True

    restarted = DriftingBasisStrategy(
        window_minutes=1,
        upper_bps=3.0,
        lower_bps=3.5,
    )
    assert restarted.state().ready is False
    assert restarted.state().center_bps is None


def test_same_observation_stream_produces_deterministic_replay_state_sequence():
    observations = [
        (1000.0 + i, -2.0 if i < 30 else 2.0)
        for i in range(61)
    ]

    live_style = DriftingBasisStrategy(
        window_minutes=1, upper_bps=3.0, lower_bps=3.5
    )
    replay_style = DriftingBasisStrategy(
        window_minutes=1, upper_bps=3.0, lower_bps=3.5
    )

    live_states = []
    replay_states = []
    for ts, premium in observations:
        live_style.update(ts, premium)
        live_states.append(live_style.state())
    for ts, premium in observations:
        replay_style.update(ts, premium)
        replay_states.append(replay_style.state())

    assert replay_states == live_states
