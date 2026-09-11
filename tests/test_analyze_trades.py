"""Standalone trade analyzer: parsing, execution quality, and reporting."""
import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import tools.analyze_trades as analyze_trades  # noqa: E402
from entropy_arb.engine import CSV_HEADER  # noqa: E402


def _trade_row(**overrides):
    row = {field: "" for field in CSV_HEADER}
    row.update({
        "ts": "1700000000.000",
        "signal_ts": "1699999999.900",
        "run_id": "run-1",
        "event_id": "event-1",
        "symbol": "SNDK",
        "hedge": "lighter-rh",
        "execution_ms": "100.000",
        "leg_settle_gap_ms": "20.000",
        "direction": "sell_entropy",
        "buy_venue": "RH",
        "sell_venue": "ENTROPY",
        "qty": "1.0",
        "buy_bbo_px": "100.0",
        "buy_bbo_qty": "2.0",
        "sell_bbo_px": "110.0",
        "sell_bbo_qty": "2.0",
        "buy_quote_age_ms": "20.0",
        "sell_quote_age_ms": "30.0",
        "buy_limit": "100.0",
        "sell_limit": "110.0",
        "buy_protect_limit": "100.5",
        "sell_protect_limit": "109.5",
        "buy_notional": "100.0",
        "sell_notional": "110.0",
        "exp_edge_usd": "10.0",
        "gross_edge_usd": "10.0",
        "marginal_premium_bps": "7.0",
        "midline_bps": "1.0",
        "inv_add_bps": "0.0",
        "buy_fill": "1.0",
        "sell_fill": "1.0",
        "buy_avg_px": "101.0",
        "sell_avg_px": "109.0",
        "matched_qty": "1.0",
        "residual_qty": "0.0",
        "buy_status": "filled",
        "sell_status": "filled",
        "unresolved": "0",
        "ok": "1",
        "error": "",
        "hedge_status": "not_needed",
        "hedge_fill": "0.0",
        "hedge_duration_ms": "0.0",
        "remaining_net_qty": "0.0",
        "fill_edge_usd": "8.0",
    })
    row.update({key: str(value) for key, value in overrides.items()})
    return row


def _write_csv(path, rows):
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def test_load_rows_parses_filters_and_counts_invalid_rows(tmp_path):
    path = tmp_path / "trades.csv"
    rows = [
        _trade_row(event_id="event-2", ts="1700000060.000"),
        _trade_row(event_id="event-1"),
        _trade_row(event_id="bad", buy_avg_px="not-a-number"),
        _trade_row(event_id="other", symbol="OAI"),
    ]
    _write_csv(path, rows)

    loaded, skipped = analyze_trades.load_rows(
        str(path), symbol="SNDK", hedge="lighter-rh")

    assert [row["event_id"] for row in loaded] == ["event-1", "event-2"]
    assert skipped == 1
    assert all(row["symbol"] == "SNDK" for row in loaded)
    assert all(row["run_id"] == "run-1" for row in loaded)


def test_summarize_trades_reports_fills_edge_slippage_and_hedging():
    rows = [
        analyze_trades.parse_row(_trade_row()),
        analyze_trades.parse_row(
            _trade_row(
                event_id="event-2",
                qty="2.0",
                buy_bbo_qty="3.0",
                sell_bbo_qty="3.0",
                buy_fill="2.0",
                sell_fill="1.0",
                buy_avg_px="100.0",
                sell_avg_px="110.0",
                matched_qty="1.0",
                residual_qty="1.0",
                exp_edge_usd="6.0",
                marginal_premium_bps="11.0",
                hedge_status="filled",
                hedge_fill="1.0",
                hedge_duration_ms="50.0",
                leg_settle_gap_ms="40.0",
                fill_edge_usd="3.0",
            ))
    ]

    summary = analyze_trades.summarize_trades(rows)

    assert summary["count"] == 2
    assert summary["ok_count"] == 2
    assert summary["fill_counts"] == {
        "full": 1, "partial": 1, "unmatched": 0, "no_fill": 0,
    }
    assert summary["matched_qty"] == pytest.approx(2.0)
    assert summary["requested_qty"] == pytest.approx(3.0)
    assert summary["matched_ratio"] == pytest.approx(2 / 3)
    assert summary["exp_edge_total"] == pytest.approx(16.0)
    assert summary["fill_edge_total"] == pytest.approx(11.0)
    assert summary["edge_capture_ratio"] == pytest.approx(11 / 16)
    assert summary["realized_edge_bps_mean"] == pytest.approx(
        ((8 / 101) * 10000 + (3 / 100) * 10000) / 2)
    assert summary["realized_edge_bps_median"] == pytest.approx(
        ((8 / 101) * 10000 + (3 / 100) * 10000) / 2)
    assert summary["buy_slippage_bps_mean"] == pytest.approx(50.0)
    assert summary["sell_slippage_bps_mean"] == pytest.approx(
        (10000 / 110 + 0) / 2)
    assert summary["buy_slippage_bps_weighted"] == pytest.approx(
        1 / 300 * 10000)
    assert summary["sell_slippage_bps_weighted"] == pytest.approx(
        1 / 220 * 10000)
    assert summary["hedge_attempts"] == 1
    assert summary["hedge_completed"] == 1
    assert summary["leg_settle_gap_ms_mean"] == pytest.approx(30.0)
    assert summary["leg_settle_gap_ms_p50"] == pytest.approx(30.0)
    assert summary["leg_settle_gap_ms_p90"] == pytest.approx(38.0)


def test_signal_groups_keep_direction_and_premium_distance_separate():
    rows = [
        analyze_trades.parse_row(_trade_row()),
        analyze_trades.parse_row(
            _trade_row(event_id="event-2", marginal_premium_bps="11.0")),
        analyze_trades.parse_row(
            _trade_row(event_id="event-3", direction="buy_entropy",
                       marginal_premium_bps="-5.0", midline_bps="1.0")),
    ]

    groups = analyze_trades.group_signal_rows(rows)

    assert groups[("sell_entropy", "6-8")] == [rows[0]]
    assert groups[("sell_entropy", "10+")] == [rows[1]]
    assert groups[("buy_entropy", "6-8")] == [rows[2]]


def test_main_prints_trade_report_and_skipped_count(tmp_path, monkeypatch,
                                                    capsys):
    path = tmp_path / "trades.csv"
    _write_csv(path, [_trade_row(),
                      _trade_row(event_id="bad", buy_avg_px="bad")])
    monkeypatch.setattr(sys, "argv", ["analyze_trades.py", "--csv", str(path)])

    analyze_trades.main()

    out = capsys.readouterr().out
    assert "trade summary" in out
    assert "execution quality" in out
    assert "leg settle gap" in out
    assert "hedge" in out
    assert "signal buckets" in out
    assert "skipped invalid rows: 1" in out


def test_default_trade_analyzer_lists_multiple_pair_files(tmp_path, monkeypatch,
                                                           capsys):
    trade_dir = tmp_path / "logs" / "trades"
    trade_dir.mkdir(parents=True)
    (trade_dir / "trades-SNDK-lighter.csv").write_text("header\n")
    (trade_dir / "trades-SNDK-lighter-rh.csv").write_text("header\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["analyze_trades.py"])

    with pytest.raises(SystemExit) as exc:
        analyze_trades.main()

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "multiple pair-specific trade files" in err
    assert "trades-SNDK-lighter.csv" in err
    assert "trades-SNDK-lighter-rh.csv" in err
