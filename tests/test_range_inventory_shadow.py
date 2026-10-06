import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.range_inventory_shadow import (  # noqa: E402
    FrozenRangeInventoryParams,
    RangeInventoryShadow,
    replay_rows,
)


def row(minute, premium, *, bid=100.0, ask=100.0, qty=100.0):
    return {
        "minute_ts": str(minute * 60),
        "time_utc": f"2026-01-01T00:{minute % 60:02d}:00Z",
        "premium_mean_bps": str(premium),
        "entropy_bid": str(bid),
        "entropy_ask": str(ask),
        "entropy_bid_qty": str(qty),
        "entropy_ask_qty": str(qty),
        "hedge_bid": str(bid),
        "hedge_ask": str(ask),
        "hedge_bid_qty": str(qty),
        "hedge_ask_qty": str(qty),
        "samples": "60",
    }


def small_params(**updates):
    values = dict(
        long_window_minutes=4,
        short_window_minutes=2,
        min_coverage_pct=100.0,
        range_gate_window_minutes=4,
        max_signal_to_execution_gap_seconds=60.0,
    )
    values.update(updates)
    return FrozenRangeInventoryParams(**values)


def test_signal_is_never_executed_in_same_minute():
    shadow = RangeInventoryShadow(
        variant="baseline", use_range_gate=False, params=small_params()
    )
    for minute, premium in enumerate([0.0, 1.0, 2.0]):
        shadow.warmup_row(row(minute, premium))

    signal = shadow.on_row(row(3, -10.0))
    assert signal.trade_notional_usd == 0.0
    assert signal.signal_target_usd is not None
    assert signal.signal_target_usd > 0.0
    assert signal.inventory_usd == 0.0

    execution = shadow.on_row(row(4, -9.0))
    assert execution.executed_signal_ts == pytest.approx(3 * 60)
    assert execution.trade_notional_usd > 0.0
    assert execution.inventory_usd > 0.0


def test_stale_pending_signal_is_not_executed_across_gap():
    shadow = RangeInventoryShadow(
        variant="baseline", use_range_gate=False, params=small_params()
    )
    for minute, premium in enumerate([0.0, 1.0, 2.0]):
        shadow.warmup_row(row(minute, premium))
    shadow.on_row(row(3, -10.0))

    result = shadow.on_row(row(5, -9.0))
    assert result.action == "stale_signal_skipped"
    assert result.trade_notional_usd == 0.0
    assert result.inventory_usd == 0.0


def test_range_gate_blocks_only_exposure_increase():
    params = small_params(range_gate_min_bps=50.0)
    shadow = RangeInventoryShadow(
        variant="range_gate", use_range_gate=True, params=params
    )
    for minute, premium in enumerate([0.0, 1.0, 2.0]):
        shadow.warmup_row(row(minute, premium))

    # Narrow range, low percentile: baseline would build long, gate blocks it.
    result = shadow.on_row(row(3, -1.0))
    assert result.signal_exposure_blocked is True
    assert result.signal_target_usd == pytest.approx(0.0)


def test_range_gate_does_not_block_reduction():
    params = small_params(range_gate_min_bps=50.0)
    shadow = RangeInventoryShadow(
        variant="range_gate", use_range_gate=True, params=params
    )
    # Put the shadow in an existing long position, then feed a narrow 4-minute
    # range whose last value is high percentile.  The range gate is closed,
    # but a reduction must still be queued.
    shadow.q_position = 30.0  # $3k at the $100 reference price
    for minute, premium in enumerate([0.0, 0.1, 0.2]):
        shadow.warmup_row(row(minute, premium))
    reduction_signal = shadow.on_row(row(3, 0.3))

    assert reduction_signal.range_gate_open is False
    assert reduction_signal.signal_target_usd is not None
    assert abs(reduction_signal.signal_target_usd) < abs(reduction_signal.inventory_usd)
    assert reduction_signal.signal_exposure_blocked is False


def test_friction_is_applied_per_leg():
    params = small_params(friction_bps_per_leg=0.5)
    shadow = RangeInventoryShadow(
        variant="baseline", use_range_gate=False, params=params
    )
    for minute, premium in enumerate([0.0, 1.0, 2.0]):
        shadow.warmup_row(row(minute, premium))
    shadow.on_row(row(3, -10.0))
    result = shadow.on_row(row(4, -9.0))

    # Equal BBOs imply zero spread cash flow.  Equity loss is friction plus
    # zero liquidation value.  0.5 bps per leg = 1.0 bps pair cost.
    expected_cost = result.trade_notional_usd * 1.0 / 10_000.0
    assert result.cash_usd == pytest.approx(-expected_cost)
    assert result.equity_usd == pytest.approx(-expected_cost)


def test_replay_starts_flat_but_inherits_prior_history():
    rows = [row(i, float(i)) for i in range(8)]
    results = replay_rows(
        rows,
        start_signal_minute_ts=6 * 60,
        params=small_params(),
    )
    assert len(results) == 4  # two variants x minutes 6 and 7
    first_baseline = next(x for x in results if x.variant == "baseline")
    assert first_baseline.inventory_usd == 0.0
    assert first_baseline.long_percentile is not None
    assert first_baseline.short_percentile is not None
