"""End-to-end offline checks for Legacy Rolling parameters in Engine.

These tests use the real Engine scan/plan/execute path with local fake venues.
They never create a venue client or send a network request.
"""
import asyncio
import csv
import json
import logging
from pathlib import Path

import pytest
from rich.console import Console

from entropy_arb.book import OrderBook
from entropy_arb.config import load_config
from entropy_arb.dashboard import BufferLogHandler, Dashboard
from entropy_arb.engine import Engine


EFFECTIVE = 1_700_031_600  # 2023-11-15T07:00:00Z
MARKET = "io:SNDK|lighter-rh:SNDK"
FIELDS = ["minute_ts", "premium_close_bps", "sell_edge_max_bps",
          "buy_edge_max_bps", "samples"]


class FakeVenue:
    def __init__(self, key, *, fill_ratio=1.0, unresolved=False,
                 reject=False):
        self.key = key
        self.name = "RH" if key == "hedge" else "ENTROPY"
        self.cap_usd = 100.0
        self.fee_bps = 0.0
        self.size_decimals = 4
        self.min_base = 0.0001
        self.min_quote = 10.0
        self.orders_per_min = 30
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0
        self.last_traded_ts = 0.0
        self.fill_ratio = fill_ratio
        self.unresolved = unresolved
        self.reject = reject
        self.calls = []
        self.book = OrderBook()

    def ready_to_trade(self):
        return True

    def set_book(self, bid, ask, size=10.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(size)}],
                            [{"px": str(ask), "sz": str(size)}]])

    def px_round(self, px, round_up=False):
        return round(px, 4)

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False,
                         trace=None):
        self.calls.append({"is_buy": is_buy, "qty": qty,
                           "limit_px": limit_px,
                           "reduce_only": reduce_only})
        if trace is not None:
            from entropy_arb.execution_journal import stamp
            trace["test_client_order_id"] = f"fake-{self.key}"
            for name in ("submit_enter", "request_start", "api_response",
                         "settlement_observed"):
                stamp(trace, name)
        if self.unresolved:
            return {"status": "timeout", "filled_base": 0.0,
                    "avg_px": None, "err": None, "unresolved": True,
                    "exchange_order_id": f"{self.key}-unknown"}
        if self.reject:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": "API rejected", "unresolved": False,
                    "exchange_order_id": None}
        filled = qty * self.fill_ratio
        return {"status": "filled" if filled else "no-fill",
                "filled_base": filled,
                "avg_px": limit_px if filled else None,
                "err": None, "unresolved": False,
                "exchange_order_id": f"{self.key}-order-{len(self.calls)}"}


def write_rows(path: Path, rows):
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def constant_rows(effective=EFFECTIVE, count=480):
    return [{"minute_ts": effective - (count - i) * 60,
             "premium_close_bps": -4.0,
             "sell_edge_max_bps": -0.5,
             "buy_edge_max_bps": 6.0,
             "samples": 60}
            for i in range(count)]


def boundary_rows(effective=EFFECTIVE, include_tail=True):
    start = effective - 8 * 3600
    rows = []
    for i in range(540):  # through effective + 00:59
        ts = start + i * 60
        high = i >= 240
        rows.append({"minute_ts": ts,
                     "premium_close_bps": 4.0 if high else -4.0,
                     "sell_edge_max_bps": 9.0 if high else -0.5,
                     "buy_edge_max_bps": 10.0 if high else 6.0,
                     "samples": 60})
    if not include_tail:
        rows.pop()  # the minute immediately before effective + 1h
    return rows


def make_config(tmp_path, csv_path, *, journal=True):
    config = tmp_path / "config.yaml"
    config.write_text(
        "strategy:\n"
        "  mode: rolling_legacy\n"
        f"  rolling_csv: '{csv_path}'\n"
        "  sell_quantile: 0.80\n"
        "  buy_quantile: 0.50\n"
        "sizing:\n"
        "  take_fraction: 0.5\n"
        "  max_order_notional_usd: 25\n"
        "  min_order_notional_usd: 10\n"
        "inventory:\n"
        "  scale_bps: 0\n"
        "execution:\n"
        "  premium_persist_sec: 0\n"
        "  cooldown_sec: 0\n"
        "  leg_slippage_bps: 0\n"
        "  hedge_slippage_bps: 0\n"
        "logging:\n"
        f"  journal_enabled: {'true' if journal else 'false'}\n"
        f"  journal_path: '{tmp_path / 'events.jsonl'}'\n"
        f"  trades_csv: '{tmp_path / 'trades.csv'}'\n"
        "  dashboard: false\n"
    )
    cfg = load_config(str(config), str(tmp_path / "missing.env"),
                      symbol="SNDK", hedge_venue="lighter-rh")
    cfg.recorder_enabled = False
    return cfg


def attach_fake_venues(engine, *, buy_ratio=1.0, sell_ratio=1.0,
                       buy_unresolved=False, buy_reject=False):
    engine.entropy = FakeVenue("entropy", fill_ratio=sell_ratio)
    engine.hedge = FakeVenue("hedge", fill_ratio=buy_ratio,
                             unresolved=buy_unresolved, reject=buy_reject)
    engine.venues = {"entropy": engine.entropy, "hedge": engine.hedge}
    engine._step = 0.0001
    engine._min_base = 0.0001
    engine._min_notional = 10.0


def sell_entropy_books(engine):
    # Executable premium is 100.00 / 100.00 - 1 = 0 bps, above M + U = -0.5.
    engine.entropy.set_book(100.00, 100.10, size=0.4)
    engine.hedge.set_book(99.90, 100.00, size=0.4)


def scan_after_arm(engine):
    async def scan():
        engine._scan(EFFECTIVE)
        return engine._scan(EFFECTIVE)
    return asyncio.run(scan())


def test_engine_rolling_snapshot_drives_scan_plan_execution_and_journal(tmp_path):
    csv_path = tmp_path / "minutes-SNDK-lighter-rh.csv"
    write_rows(csv_path, constant_rows())
    cfg = make_config(tmp_path, csv_path)
    engine = Engine(cfg)
    attach_fake_venues(engine)

    async def execute():
        engine.journal.start()
        assert engine._refresh_rolling(EFFECTIVE) is True
        assert engine._threshold_values() == (-4.0, 3.5, 2.0)
        assert engine._eff_threshold(engine.hedge, engine.entropy) == -0.5
        assert engine._eff_threshold(engine.entropy, engine.hedge) == 6.0
        sell_entropy_books(engine)
        engine._scan(EFFECTIVE)
        plan_result = engine._scan(EFFECTIVE)
        assert plan_result is not None
        buy, sell, plan = plan_result
        assert (buy.key, sell.key) == ("hedge", "entropy")
        assert plan.qty == 0.2  # 50% of 0.4 visible crossable depth
        assert plan.buy_notional == 20.0
        assert plan.sell_notional == 20.0
        await engine._vlock(buy.key).acquire()
        await engine._vlock(sell.key).acquire()
        await engine._execute_locked(buy, sell, plan)
        await engine.journal.close()

    asyncio.run(execute())
    assert engine.hedge.position == 0.2
    assert engine.entropy.position == -0.2
    assert len(engine.hedge.calls) == len(engine.entropy.calls) == 1
    assert engine.hedge.calls[0]["reduce_only"] is False

    events = [json.loads(line)
              for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    snapshot = next(e for e in events if e["event_type"] == "parameter_snapshot")
    decision = next(e for e in events if e["event_type"] == "decision")
    assert (snapshot["midline_bps"], snapshot["upper_bps"], snapshot["lower_bps"]) == (-4.0, 3.5, 2.0)
    assert snapshot["effective_at"] == EFFECTIVE
    assert snapshot["coverage"] == 1.0 and snapshot["valid"] is True
    assert (decision["midline_bps"], decision["upper_bps"], decision["lower_bps"]) == (-4.0, 3.5, 2.0)
    assert decision["direction"] == "sell_entropy"
    assert any(e["event_type"] == "execution_complete" and e["success"]
               for e in events)
    with (tmp_path / "trades.csv").open(newline="") as fh:
        trade = next(csv.DictReader(fh))
    assert trade["direction"] == "sell_entropy"
    assert float(trade["midline_bps"]) == -4.0


@pytest.mark.parametrize(
    ("direction", "entropy_book", "hedge_book", "expected"),
    [
        ("sell_entropy", (100.00, 100.10), (99.90, 100.00),
         ("hedge", "entropy", -0.5)),
        ("buy_entropy", (99.90, 100.00), (100.10, 100.20),
         ("entropy", "hedge", 6.0)),
    ],
)
def test_engine_rolling_selects_both_signed_signal_directions(
        tmp_path, direction, entropy_book, hedge_book, expected):
    csv_path = tmp_path / f"{direction}.csv"
    write_rows(csv_path, constant_rows())
    engine = Engine(make_config(tmp_path, csv_path, journal=False))
    attach_fake_venues(engine)
    assert engine._refresh_rolling(EFFECTIVE) is True
    engine.entropy.set_book(*entropy_book, size=0.4)
    engine.hedge.set_book(*hedge_book, size=0.4)

    async def scan():
        engine._scan(EFFECTIVE)
        return engine._scan(EFFECTIVE)

    result = asyncio.run(scan())
    assert result is not None
    buy, sell, plan = result
    assert (buy.key, sell.key) == expected[:2]
    assert engine._eff_threshold(buy, sell) == expected[2]
    assert plan.qty == 0.2

    # Values below either signed hurdle must not arm a plan.
    if direction == "buy_entropy":
        engine = Engine(make_config(tmp_path, csv_path, journal=False))
        attach_fake_venues(engine)
        assert engine._refresh_rolling(EFFECTIVE) is True
        engine.entropy.set_book(99.90, 100.00, size=0.4)
        engine.hedge.set_book(100.05, 100.15, size=0.4)
        assert scan_after_arm(engine) is None
    else:
        engine = Engine(make_config(tmp_path, csv_path, journal=False))
        attach_fake_venues(engine)
        assert engine._refresh_rolling(EFFECTIVE) is True
        engine.entropy.set_book(99.90, 100.10, size=0.4)
        engine.hedge.set_book(99.90, 100.00, size=0.4)
        assert scan_after_arm(engine) is None


def test_rolling_boundary_is_causal_and_fails_closed_until_tail_arrives(tmp_path):
    csv_path = tmp_path / "boundary.csv"
    write_rows(csv_path, boundary_rows(include_tail=False))
    engine = Engine(make_config(tmp_path, csv_path, journal=False))
    attach_fake_venues(engine)

    # Before 08:00 UTC effective boundary, the old 8h window is valid.
    assert engine._refresh_rolling(EFFECTIVE + 3599) is True
    assert engine._active_rolling.effective_at == EFFECTIVE
    assert engine._active_rolling.midline_bps == 0.0

    # At the next boundary the newest completed minute is absent: the old
    # snapshot must not survive as a trading parameter.
    assert engine._refresh_rolling(EFFECTIVE + 3600) is False
    assert engine._active_rolling.valid is False
    assert engine._active_rolling.invalid_reason == "data_gap_or_insufficient_window"
    assert engine._threshold_values() is None

    # The row at the effective boundary is future data and is excluded.
    rows = boundary_rows(include_tail=True)
    rows.append({"minute_ts": EFFECTIVE + 3600,
                 "premium_close_bps": 999.0,
                 "sell_edge_max_bps": 999.0,
                 "buy_edge_max_bps": 999.0,
                 "samples": 60})
    write_rows(csv_path, rows)
    assert engine._refresh_rolling(EFFECTIVE + 3600) is True
    assert engine._active_rolling.midline_bps == 4.0
    assert engine._active_rolling.sample_count == 480


def test_invalid_rolling_snapshot_blocks_real_engine_evaluation(tmp_path):
    csv_path = tmp_path / "short.csv"
    write_rows(csv_path, constant_rows(count=479))
    engine = Engine(make_config(tmp_path, csv_path, journal=False))
    attach_fake_venues(engine)
    sell_entropy_books(engine)
    called = []
    engine._scan = lambda now: called.append(now)
    asyncio.run(engine._evaluate())
    assert called == []
    assert engine._active_rolling is not None
    assert not engine._active_rolling.valid
    assert engine._threshold_values() is None


def test_rolling_dashboard_shows_active_parameters_and_invalid_state(tmp_path):
    csv_path = tmp_path / "dashboard.csv"
    write_rows(csv_path, constant_rows())
    engine = Engine(make_config(tmp_path, csv_path, journal=False))
    attach_fake_venues(engine)
    engine._refresh_rolling(EFFECTIVE)
    dashboard = Dashboard(engine, BufferLogHandler(), "unused.log")
    console = Console(record=True, width=160, force_terminal=True)
    console.print(dashboard._signal_panel())
    rendered = console.export_text()
    assert "midline -4.00" in rendered
    assert "[-6.00 … -0.50]" in rendered
    assert "hurdle bps" in rendered
    assert "+6.0" in rendered
    assert "+2.00" not in rendered

    missing = tmp_path / "missing.csv"
    engine._rolling.path = missing
    assert engine._refresh_rolling(EFFECTIVE + 3600) is False
    console = Console(record=True, width=160, force_terminal=True)
    console.print(dashboard._signal_panel())
    rendered = console.export_text()
    assert "midline —" in rendered
    assert "band INVALID" in rendered
    assert "hurdle bps" in rendered
    assert "+2.00" not in rendered


def test_status_does_not_render_invalid_rolling_band_as_zero(caplog, tmp_path,
                                                              monkeypatch):
    csv_path = tmp_path / "status.csv"
    write_rows(csv_path, constant_rows(count=479))
    engine = Engine(make_config(tmp_path, csv_path, journal=False))
    attach_fake_venues(engine)
    assert engine._refresh_rolling(EFFECTIVE) is False

    async def stop_after_sleep(_):
        engine.stop.set()

    monkeypatch.setattr(asyncio, "sleep", stop_after_sleep)
    with caplog.at_level(logging.INFO, logger="engine"):
        asyncio.run(engine._status_loop())
    assert any("band INVALID" in record.getMessage()
               for record in caplog.records)


def test_status_uses_active_rolling_band(caplog, tmp_path, monkeypatch):
    csv_path = tmp_path / "status-valid.csv"
    write_rows(csv_path, constant_rows())
    engine = Engine(make_config(tmp_path, csv_path, journal=False))
    attach_fake_venues(engine)
    assert engine._refresh_rolling(EFFECTIVE) is True

    async def stop_after_sleep(_):
        engine.stop.set()

    monkeypatch.setattr(asyncio, "sleep", stop_after_sleep)
    with caplog.at_level(logging.INFO, logger="engine"):
        asyncio.run(engine._status_loop())
    assert any("band -6.00..-0.50" in record.getMessage()
               for record in caplog.records)

