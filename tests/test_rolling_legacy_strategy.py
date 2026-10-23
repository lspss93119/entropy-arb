"""Isolation and causal calculation tests for the opt-in Legacy Rolling strategy."""
import asyncio
import csv
from types import SimpleNamespace

from entropy_arb.rolling_legacy_strategy import (
    LegacyRollingStrategy, calculate_legacy,
)
from entropy_arb.engine import Engine

EFFECTIVE = 1_700_031_600
MARKET = "io:SNDK|lighter-rh:SNDK"


def rows(n=480):
    start = EFFECTIVE - n * 60
    return [
        {"ts": start + i * 60, "market": MARKET,
         "premium": 10.0 if i % 2 else 0.0,
         "sell": 18.0 if i % 2 else 12.0,
         "buy": -4.0 if i % 2 else 0.0,
         "samples": 30}
        for i in range(n)
    ]


def test_legacy_p80_p50_uses_executable_room():
    snap = calculate_legacy(rows(), EFFECTIVE, MARKET)
    assert snap.valid
    assert snap.midline_bps == 5.0
    # sell room 7 or 13; P80 -> 13; buy room 1 or 5; P50 -> 3
    assert snap.upper_bps == 13.0
    assert snap.lower_bps == 3.0


def test_legacy_fees_are_subtracted_once_before_band_rounding():
    snap = calculate_legacy(rows(), EFFECTIVE, MARKET, fees_bps=1.0)
    assert snap.valid
    # SellRoom is 6 or 12 -> P80 is 12; BuyRoom is 0 or 4 -> P50 is 2.
    assert snap.upper_bps == 12.0
    assert snap.lower_bps == 2.0


def test_legacy_rejects_nonfinite_samples_and_bad_timestamps():
    bad_samples = rows()
    bad_samples[0]["samples"] = float("nan")
    result = calculate_legacy(bad_samples, EFFECTIVE, MARKET)
    assert not result.valid
    assert result.invalid_reason == "invalid_or_low_samples"

    fractional_samples = rows()
    fractional_samples[0]["samples"] = 10.5
    result = calculate_legacy(fractional_samples, EFFECTIVE, MARKET)
    assert not result.valid
    assert result.invalid_reason == "invalid_or_low_samples"

    bad_timestamp = rows()
    bad_timestamp[0]["ts"] = "not-a-timestamp"
    result = calculate_legacy(bad_timestamp, EFFECTIVE, MARKET)
    assert not result.valid
    assert result.invalid_reason == "invalid_timestamp"


def test_window_gap_and_low_samples_fail_closed():
    assert not calculate_legacy(rows(479), EFFECTIVE, MARKET).valid
    missing = rows()
    del missing[100]
    assert not calculate_legacy(missing, EFFECTIVE, MARKET).valid
    low = rows()
    low[0]["samples"] = 9
    assert not calculate_legacy(low, EFFECTIVE, MARKET).valid


def test_market_mismatch_rejected():
    mixed = rows()
    mixed[4]["market"] = "other"
    assert calculate_legacy(mixed, EFFECTIVE, MARKET).invalid_reason == "market_mismatch"


def test_provider_reads_csv_and_rejects_missing_history(tmp_path):
    p = tmp_path / "sndk-rh.csv"
    with p.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "minute_ts", "premium_close_bps", "sell_edge_max_bps",
            "buy_edge_max_bps", "samples"])
        writer.writeheader()
        for r in rows():
            writer.writerow({"minute_ts": r["ts"],
                             "premium_close_bps": r["premium"],
                             "sell_edge_max_bps": r["sell"],
                             "buy_edge_max_bps": r["buy"],
                             "samples": r["samples"]})
    strategy = LegacyRollingStrategy(csv_path=str(p), market_key=MARKET)
    assert strategy.refresh(EFFECTIVE).valid
    assert strategy.refresh(EFFECTIVE + 3600).valid is False


def test_provider_rejects_mismatched_csv_market_identity(tmp_path):
    p = tmp_path / "sndk-rh.csv"
    with p.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "minute_ts", "market_key", "premium_close_bps",
            "sell_edge_max_bps", "buy_edge_max_bps", "samples"])
        writer.writeheader()
        for r in rows():
            writer.writerow({"minute_ts": r["ts"],
                             "market_key": "wrong-market" if r["ts"] == EFFECTIVE - 60 else MARKET,
                             "premium_close_bps": r["premium"],
                             "sell_edge_max_bps": r["sell"],
                             "buy_edge_max_bps": r["buy"],
                             "samples": r["samples"]})
    strategy = LegacyRollingStrategy(csv_path=str(p), market_key=MARKET)
    result = strategy.refresh(EFFECTIVE)
    assert not result.valid
    assert result.invalid_reason == "market_mismatch"


def test_provider_does_not_use_incomplete_tail_and_retries_after_flush(tmp_path):
    p = tmp_path / "sndk-rh.csv"
    with p.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "minute_ts", "premium_close_bps", "sell_edge_max_bps",
            "buy_edge_max_bps", "samples"])
        writer.writeheader()
        for r in rows():
            writer.writerow({"minute_ts": r["ts"],
                             "premium_close_bps": r["premium"],
                             "sell_edge_max_bps": r["sell"],
                             "buy_edge_max_bps": r["buy"],
                             "samples": r["samples"]})
    complete = p.read_bytes()
    assert complete.endswith(b"\n")
    p.write_bytes(complete[:-1])

    strategy = LegacyRollingStrategy(csv_path=str(p), market_key=MARKET)
    first = strategy.refresh(EFFECTIVE)
    assert not first.valid
    assert first.invalid_reason == "incomplete_csv_tail"

    p.write_bytes(complete)
    second = strategy.refresh(EFFECTIVE)
    assert second.valid


def test_provider_rejects_strict_csv_parse_errors(tmp_path):
    p = tmp_path / "sndk-rh.csv"
    p.write_text(
        "minute_ts,premium_close_bps,sell_edge_max_bps,buy_edge_max_bps,samples\n"
        '"unterminated,0,0,0,30\n'
    )
    strategy = LegacyRollingStrategy(csv_path=str(p), market_key=MARKET)
    result = strategy.refresh(EFFECTIVE)
    assert not result.valid
    assert result.invalid_reason == "csv_read_error:Error"


def test_fixed_strategy_keeps_original_threshold_without_rolling_provider():
    cfg = SimpleNamespace(strategy_mode="fixed", midline_bps=10.0,
                          upper_bps=4.0, lower_bps=5.0,
                          journal_path="unused", journal_enabled=False)
    eng = Engine(cfg, record_only=False)
    assert eng._rolling is None
    assert eng._threshold_values() == (10.0, 4.0, 5.0)
    buy = SimpleNamespace(key="hedge")
    sell = SimpleNamespace(key="entropy")
    eng._inv_add_bps = lambda *_: 0.0
    assert eng._eff_threshold(buy, sell) == 14.0
    assert eng._eff_threshold(sell, buy) == -5.0


def test_rolling_strategy_never_falls_back_to_fixed_config(tmp_path):
    cfg = SimpleNamespace(strategy_mode="rolling_legacy",
                          rolling_csv=str(tmp_path / "missing.csv"),
                          rolling_sell_quantile=0.8,
                          rolling_buy_quantile=0.5,
                          entropy=SimpleNamespace(hl_dex="io", fee_bps=0),
                          hedge=SimpleNamespace(fee_bps=0),
                          symbol="SNDK", hedge_venue="lighter-rh",
                          midline_bps=999.0, upper_bps=999.0, lower_bps=999.0,
                          journal_path="unused", journal_enabled=False)
    eng = Engine(cfg, record_only=False)
    assert eng._threshold_values() is None
    assert eng._refresh_rolling(EFFECTIVE) is False
    assert eng._threshold_values() is None


def test_invalid_rolling_snapshot_blocks_engine_scan(tmp_path):
    cfg = SimpleNamespace(strategy_mode="rolling_legacy",
                          rolling_csv=str(tmp_path / "missing.csv"),
                          rolling_sell_quantile=0.8,
                          rolling_buy_quantile=0.5,
                          entropy=SimpleNamespace(hl_dex="io", fee_bps=0),
                          hedge=SimpleNamespace(fee_bps=0),
                          symbol="SNDK", hedge_venue="lighter-rh",
                          journal_path="unused", journal_enabled=False)
    eng = Engine(cfg, record_only=False)
    called = []
    eng._scan = lambda now: called.append(now)
    asyncio.run(eng._evaluate())
    assert called == []
