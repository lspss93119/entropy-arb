"""Regression tests for causal signal-only P1 study."""
from datetime import datetime, timedelta, timezone
from research.p1_midline_shadow import shadow


START = datetime(2026, 10, 18, tzinfo=timezone.utc)


def rows(values, step=1):
    return [{"time": START + timedelta(minutes=i * step),
             "close": v, "sell": v + 3, "buy": -v + 3}
            for i, v in enumerate(values)]


def test_fixed_training_center_never_moves():
    result = shadow(rows([0, 2, 100, 100, 100]), 2, windows=(2,))
    assert [r["fixed_midline"] for r in result] == [1, 1, 1]


def test_rolling_excludes_current_close_and_no_lookahead():
    result = shadow(rows([1, 1, 50, 80, 80]), 2, windows=(2,))
    assert result[0]["rolling_2m_midline"] == 1
    assert result[1]["rolling_2m_midline"] == 25.5


def test_gap_invalidates_rolling_history():
    data = rows([1, 1, 1, 1])
    data[3]["time"] += timedelta(minutes=10)
    result = shadow(data, 2, windows=(2,))
    assert result[0]["rolling_2m_midline"] == 1
    assert result[1]["rolling_2m_midline"] is None


def test_asymmetric_executable_thresholds():
    data = rows([0, 0, 0])
    data[-1]["sell"] = 2.1
    data[-1]["buy"] = 1.9
    result = shadow(data, 2, windows=(2,), upper=2, lower=2)
    assert result[0]["fixed_sell_signal"] is True
    assert result[0]["fixed_buy_signal"] is False


def test_no_early_rolling_without_history():
    result = shadow(rows([0, 0, 0, 0]), 1, windows=(60,))
    assert all(r["rolling_60m_midline"] is None for r in result)
