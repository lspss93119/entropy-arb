from __future__ import annotations

import math

import pytest

from entropy_arb.rolling_center_state import RollingCenterStateStore
from entropy_arb.strategy import (
    RollingCenterSnapshot,
    StableBasisStrategy,
)


WINDOW_SEC = 12 * 3600.0
BUCKET_SEC = 60.0


def observations(now: float, minutes: int, value: float = -5.0):
    start = now - WINDOW_SEC
    return [
        (start + (index + 0.5) * BUCKET_SEC, value)
        for index in range(minutes)
    ]


def rolling(**kwargs) -> StableBasisStrategy:
    return StableBasisStrategy(
        center_bps=-1.8,
        upper_bps=0.75,
        lower_bps=0.75,
        center_mode="rolling",
        center_window_hours=12,
        center_update_minutes=60,
        **kwargs,
    )


def test_full_window_uses_fresh_median_and_reports_coverage():
    now = 100_000.0
    strategy = rolling()

    update = strategy.bootstrap(observations(now, 720), now=now)

    assert update is not None
    assert update.coverage_ratio == pytest.approx(1.0)
    assert strategy.state().center_bps == pytest.approx(-5.0)
    assert strategy.state().center_source == "fresh_rolling"


def test_9_8_hours_gap_aware_coverage_passes_eighty_percent():
    now = 100_000.0
    strategy = rolling()

    update = strategy.bootstrap(observations(now, 588), now=now)

    assert update is not None
    assert update.coverage_ratio == pytest.approx(588 / 720)
    assert strategy.state().center_source == "fresh_rolling"


def test_79_percent_coverage_uses_fixed_fallback():
    now = 100_000.0
    strategy = rolling()

    update = strategy.bootstrap(observations(now, 568), now=now)

    assert update is None
    assert strategy.state().center_bps == pytest.approx(-1.8)
    assert strategy.state().center_source == "fixed_fallback"
    assert strategy.state().coverage_ratio == pytest.approx(568 / 720)


def test_internal_gap_reduces_coverage_even_when_timestamp_span_is_full():
    now = 100_000.0
    strategy = rolling()
    values = observations(now, 720)
    del values[240:480]

    update = strategy.bootstrap(values, now=now)

    assert update is None
    assert strategy.state().coverage_ratio == pytest.approx(480 / 720)


def test_sparse_samples_do_not_pass_from_wide_timestamp_span():
    now = 100_000.0
    strategy = rolling()

    update = strategy.bootstrap(
        [(now - WINDOW_SEC + 1.0, -5.0), (now - 1.0, -5.0)],
        now=now,
    )

    assert update is None
    assert strategy.state().coverage_ratio == pytest.approx(2 / 720)


def test_minimum_sample_count_is_explicitly_enforced():
    now = 100_000.0
    strategy = rolling(center_min_samples=721)

    assert strategy.bootstrap(observations(now, 720), now=now) is None
    assert strategy.state().center_source == "fixed_fallback"


def test_window_is_causal_and_strictly_bounded():
    now = 100_000.0
    strategy = rolling()
    values = observations(now, 720)
    values.append((now, 10_000.0))
    values.append((now + 1.0, 20_000.0))
    values.insert(0, (now - WINDOW_SEC - 1.0, 30_000.0))

    update = strategy.bootstrap(values, now=now)

    assert update is not None
    assert update.new_center_bps == pytest.approx(-5.0)


def test_persisted_last_valid_center_is_used_when_history_is_short(tmp_path):
    now = 100_000.0
    path = tmp_path / "rolling-center.json"
    snapshot = RollingCenterSnapshot(
        center_bps=-5.72,
        calculated_at=now - 2 * 3600.0,
        window_start=now - 14 * 3600.0,
        window_end=now - 2 * 3600.0,
        coverage_ratio=0.84,
        sample_count=600,
    )
    store = RollingCenterStateStore(path)
    store.save(snapshot)
    restored = store.load()
    assert restored == snapshot

    strategy = rolling()
    assert strategy.restore_last_valid(restored, now=now)
    assert strategy.bootstrap(observations(now, 400), now=now) is None
    assert strategy.state().center_bps == pytest.approx(-5.72)
    assert strategy.state().center_source == "last_valid"


def test_old_persisted_center_is_rejected_and_fallback_remains_fixed():
    now = 100_000.0
    strategy = rolling()
    snapshot = RollingCenterSnapshot(
        center_bps=-5.72,
        calculated_at=now - 7 * 3600.0,
        window_start=now - 19 * 3600.0,
        window_end=now - 7 * 3600.0,
        coverage_ratio=0.84,
        sample_count=600,
    )

    assert not strategy.restore_last_valid(snapshot, now=now)
    assert strategy.state().center_bps == pytest.approx(-1.8)
    assert strategy.state().center_source == "fixed_fallback"


def test_hourly_update_switches_last_valid_to_fresh_after_coverage_recovers():
    now = 100_000.0
    strategy = rolling(center_last_valid_max_age_hours=24.0)
    snapshot = RollingCenterSnapshot(
        center_bps=-5.72,
        calculated_at=now - 3600.0,
        window_start=now - 13 * 3600.0,
        window_end=now - 3600.0,
        coverage_ratio=0.84,
        sample_count=600,
    )
    assert strategy.restore_last_valid(snapshot, now=now)
    assert strategy.bootstrap(observations(now, 400), now=now) is None
    assert strategy.state().center_source == "last_valid"

    fresh_update = None
    for index in range(1, 721):
        fresh_update = strategy.update(
            now + index * 60.0 - 30.0,
            1.0,
        ) or fresh_update
    fresh_update = strategy.update(now + 12 * 3600.0, 1.0) or fresh_update

    assert fresh_update is not None
    assert strategy.state().center_source == "fresh_rolling"
    assert strategy.state().center_bps == pytest.approx(1.0)


def test_atomic_state_file_rejects_malformed_or_nonfinite_payload(tmp_path):
    path = tmp_path / "rolling-center.json"
    path.write_text('{"center_bps": NaN}')

    assert RollingCenterStateStore(path).load() is None


def test_coverage_bucket_boundaries_are_finite():
    now = 100_000.0
    strategy = rolling()
    update = strategy.bootstrap(observations(now, 720), now=now)

    assert update is not None
    assert math.isfinite(update.coverage_ratio)
