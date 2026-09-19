"""Walk-forward rolling-window signal calculations."""
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
    values = values or [float((minute % 2) * 2 - 1)
                        for minute in range(60)]
    for minute, value in enumerate(values):
        window.ingest_row(_row(minute * 60, value))
    return window


def test_snapshot_excludes_current_update_block():
    window = _filled_window()
    window.ingest_row(_row(60 * 60, 1000.0))

    snapshot = window.snapshot_for(60 * 60)

    assert snapshot.valid
    assert snapshot.mean_bps == 0.0
    assert snapshot.valid_minutes == 60


def test_snapshot_requires_coverage_and_positive_dispersion():
    config = RollingConf(window_hours=1, update_minutes=15,
                         min_coverage_pct=80)
    window = RollingWindow(config)
    for minute in range(10):
        window.ingest_row(_row(minute * 60, float(minute)))
    assert not window.snapshot_for(60 * 60).valid

    filled = _filled_window(config=RollingConf(window_hours=1,
                                               update_minutes=15,
                                               min_coverage_pct=80))
    snapshot = filled.snapshot_for(60 * 60)
    assert snapshot.valid

    flat = _filled_window(config=RollingConf(window_hours=1,
                                             update_minutes=15,
                                             min_coverage_pct=80),
                          values=[3.0] * 60)
    zero = flat.snapshot_for(60 * 60)
    assert not zero.valid
    assert zero.reason == "zero standard deviation"


def test_entry_signal_applies_z_reversion_and_spread_gates():
    config = RollingConf(window_hours=1, update_minutes=15,
                         min_coverage_pct=80, entry_z=1.5,
                         min_reversion_bps=5.0, max_spread_bps=10.0)
    window = _filled_window(config=config,
                            values=[float(i % 2) for i in range(60)])
    snapshot = window.snapshot_for(60 * 60)
    assert snapshot.valid

    sell = window.entry_signal(snapshot.mean_bps + 5.0, 3600.0, 5.0, 5.0)
    assert sell is not None
    assert sell.direction == "sell_entropy"
    assert sell.reason == "entry"
    assert sell.snapshot_ts == 3600.0

    assert window.entry_signal(snapshot.mean_bps + 5.0, 3600.0,
                               10.01, 5.0) is None
    assert window.entry_signal(snapshot.mean_bps + 1.0, 3600.0,
                               5.0, 5.0) is None


def test_entry_signal_negative_z_is_buy_entropy():
    config = RollingConf(window_hours=1, update_minutes=15,
                         min_coverage_pct=80, entry_z=1.0,
                         min_reversion_bps=1.0)
    window = _filled_window(config=config,
                            values=[float(i % 2) for i in range(60)])
    snapshot = window.snapshot_for(3600.0)
    signal = window.entry_signal(snapshot.mean_bps - 2.0, 3600.0, 1.0, 1.0)
    assert signal is not None
    assert signal.direction == "buy_entropy"


def test_exit_signal_uses_z_band_and_timeout():
    config = RollingConf(window_hours=1, update_minutes=15,
                         min_coverage_pct=80, entry_z=1.5, exit_z=0.5,
                         timeout_hours=1)
    window = _filled_window(config=config,
                            values=[float(i % 2) for i in range(60)])
    snapshot = window.snapshot_for(3600.0)

    exit_signal = window.exit_signal(snapshot.mean_bps, 3600.0,
                                      5.0, 5.0, 3500.0,
                                      "sell_entropy")
    assert exit_signal is not None
    assert exit_signal.reason == "exit_z"
    assert exit_signal.direction == "buy_entropy"

    timeout = window.exit_signal(snapshot.mean_bps + 100.0, 3600.0,
                                  50.0, 50.0, 0.0, "sell_entropy")
    assert timeout is not None
    assert timeout.reason == "timeout"
    assert timeout.direction == "buy_entropy"


def test_invalid_snapshot_blocks_entry_and_timeout_exits():
    config = RollingConf(window_hours=1, update_minutes=15,
                         min_coverage_pct=100, timeout_hours=1)
    window = RollingWindow(config)

    assert window.entry_signal(10.0, now=3600.0,
                               entropy_spread_bps=1.0,
                               hedge_spread_bps=1.0) is None
    signal = window.exit_signal(10.0, now=3600.0,
                                entropy_spread_bps=50.0,
                                hedge_spread_bps=50.0,
                                entry_ts=0.0,
                                direction="sell_entropy")
    assert signal is not None and signal.reason == "timeout"


def test_load_csv_seeds_and_replaces_duplicate_minute(tmp_path):
    path = tmp_path / "minutes.csv"
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "minute_ts", "samples", "premium_close_bps"])
        writer.writeheader()
        writer.writerow(_row(0, 1.0))
        writer.writerow(_row(60, 2.0))

    window = RollingWindow(RollingConf(window_hours=1, min_coverage_pct=1))
    window.load_csv(str(path))
    window.ingest_row(_row(60, 4.0))
    assert window.points == ((0.0, 1.0), (60.0, 4.0))


def test_invalid_parameters_are_rejected():
    with pytest.raises(ValueError, match="exit_z"):
        RollingWindow(RollingConf(entry_z=1.0, exit_z=1.0))
