"""Causal rolling-median snapshots and dynamic-center signals."""
import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import RollingConf  # noqa: E402
from entropy_arb.rolling import RollingWindow  # noqa: E402


def _row(minute_ts, premium, samples=1):
    return {
        "minute_ts": str(minute_ts),
        "samples": str(samples),
        "premium_close_bps": str(premium),
    }


def _filled_window(config=None, values=None):
    config = config or RollingConf(window_hours=1, update_minutes=15,
                                   min_coverage_pct=50)
    window = RollingWindow(config)
    values = values if values is not None else list(range(60))
    for minute, value in enumerate(values):
        window.ingest_row(_row(minute * 60, value))
    return window


def test_snapshot_is_causal_and_uses_median():
    window = _filled_window()
    window.ingest_row(_row(60 * 60, 10_000.0))

    snapshot = window.snapshot_for(60 * 60)

    assert snapshot.valid
    assert snapshot.median_bps == 29.5
    assert snapshot.valid_minutes == 60
    assert snapshot.block_start_ts == 3600.0


def test_snapshot_accepts_flat_data():
    window = _filled_window(values=[3.0] * 60)

    snapshot = window.snapshot_for(60 * 60)

    assert snapshot.valid
    assert snapshot.median_bps == 3.0
    assert snapshot.reason == "quality ok"


def test_snapshot_requires_coverage_and_rejects_nonpositive_samples():
    config = RollingConf(window_hours=1, update_minutes=15,
                         min_coverage_pct=80)
    window = RollingWindow(config)
    for minute in range(10):
        window.ingest_row(_row(minute * 60, float(minute)))
    assert not window.snapshot_for(60 * 60).valid
    assert "coverage" in window.snapshot_for(60 * 60).reason

    assert not window.ingest_row(_row(10 * 60, 1.0, samples=0))
    assert not window.ingest_row(_row(11 * 60, 1.0, samples=-1))


def test_duplicate_minute_replaces_old_value_and_invalidates_cache():
    window = RollingWindow(RollingConf(window_hours=1, min_coverage_pct=1))
    window.ingest_row(_row(0, 1.0))
    window.ingest_row(_row(60, 2.0))
    first = window.snapshot_for(120)

    window.ingest_row(_row(60, 8.0))
    second = window.snapshot_for(120)

    assert window.points == ((0.0, 1.0), (60.0, 8.0))
    assert first.median_bps == 1.5
    assert second.median_bps == 4.5


@pytest.mark.parametrize(
    ("premium", "expected"),
    [(10.0, "sell_entropy"), (0.0, "buy_entropy"),
     (5.0, None), (10.1, "sell_entropy")],
)
def test_signal_uses_dynamic_median_and_fixed_bands(premium, expected):
    window = _filled_window(
        RollingConf(window_hours=1, update_minutes=15, min_coverage_pct=80),
        values=[5.0] * 60)

    signal = window.signal(premium, 3600.0, upper_bps=5.0, lower_bps=5.0)

    assert (signal.direction if signal else None) == expected
    if signal:
        assert signal.reason == "entry"
        assert signal.center_bps == 5.0
        assert signal.snapshot_ts == 3600.0


def test_invalid_snapshot_blocks_signal_without_fixed_fallback():
    window = RollingWindow(RollingConf(window_hours=1, min_coverage_pct=100))
    for minute in range(10):
        window.ingest_row(_row(minute * 60, 1.0))

    assert window.signal(10.0, 3600.0, upper_bps=1.0,
                         lower_bps=1.0) is None


def test_load_csv_seeds_completed_rows(tmp_path):
    path = tmp_path / "minutes.csv"
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "minute_ts", "samples", "premium_close_bps"])
        writer.writeheader()
        writer.writerow(_row(0, 1.0))
        writer.writerow(_row(60, 2.0))

    window = RollingWindow(RollingConf(window_hours=1,
                                       min_coverage_pct=1))
    assert window.load_csv(str(path)) == 2
    assert window.points == ((0.0, 1.0), (60.0, 2.0))


def test_invalid_parameters_are_rejected():
    with pytest.raises(ValueError, match="min_exit_capture_bps"):
        RollingConf(min_exit_capture_bps=-0.1)
