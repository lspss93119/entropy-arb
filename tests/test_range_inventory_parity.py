"""Parity runner must reject target/action drift, not just compare totals."""
import importlib.util
from dataclasses import replace

import pytest

from entropy_arb.range_inventory_live import CANARY_PARAMS
from test_range_inventory_shadow import row, small_params


def runner():
    assert importlib.util.find_spec("tools.check_range_inventory_parity"), "parity runner missing"
    from tools import check_range_inventory_parity
    return check_range_inventory_parity


def test_full_fields_and_live_target_action_match_old_oracle():
    tool = runner()
    rows = [row(i, (-1 if i % 40 < 20 else 1) * i, bid=100, ask=100.1, qty=2)
            for i in range(240)]
    result = tool.check_parity(rows, params=small_params())
    assert result["historical_variant_rows"] == 480
    assert result["live_target_action_rows"] == 240
    assert result["divergences"] == 0
    canary = tool.check_parity(rows, params=replace(CANARY_PARAMS,
        long_window_minutes=4, short_window_minutes=2, range_gate_window_minutes=4,
        min_coverage_pct=100))
    assert canary["divergences"] == 0


@pytest.mark.parametrize("field,bad", [("action", "short_cover"),
                                      ("signal_target_usd", 100), ("equity_usd", 1)])
def test_any_action_target_or_accounting_drift_fails(field, bad):
    tool = runner()
    expected = {"minute_ts": 60, "action": "none", "signal_target_usd": 0,
                "equity_usd": 0}
    actual = dict(expected, **{field: bad})
    with pytest.raises(AssertionError, match=field):
        tool.compare_fields(expected, actual)


def test_live_adapter_is_actual_signal_path(tmp_path):
    tool = runner()
    from entropy_arb.range_inventory_live import RangeInventoryLive
    live = RangeInventoryLive(str(tmp_path / "unused.json"), symbol="ANTH",
                              hedge="lighter-rh", params=small_params())
    assert hasattr(live, "signal_for_inventory"), "offline parity must call the actual adapter signal path"
    for i in range(3):
        live.core.warmup_row(row(i, i))
    signal = live.signal_for_inventory(row(3, -20), inventory_usd=0)
    assert signal.target_usd > 0
    assert signal.action == "long_build"
    assert tool.expected_signal_action(0, 10, False) == "long_build"
    assert tool.expected_signal_action(10, 0, False) == "long_release"
    assert tool.expected_signal_action(-10, 0, False) == "short_cover"
    assert not (tmp_path / "unused.json").exists()
