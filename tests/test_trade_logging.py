"""Execution logging keeps the small set of fields needed for strategy research."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import ArbPlan, OrderBook
from entropy_arb.config import load_config
from entropy_arb.engine import CSV_HEADER, Engine


NO_ENV = "/tmp/entropy-arb-no-such.env"


class StubVenue:
    def __init__(self, key, name, responses, delay=0.0):
        self.key = key
        self.name = name
        self.book = OrderBook()
        self._responses = list(responses)
        self.delay = delay
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0
        self.equity = None
        self.free = None
        self.start_equity = None
        self.fee_bps = 0.0
        self.cap_usd = 1000.0
        self.orders_per_min = 30
        self.min_base = 0.1
        self.min_quote = 10.0
        self.last_traded_ts = 0.0

    def px_round(self, px, round_up):
        return px

    async def send_taker(self, **_kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._responses.pop(0)


def _book(bid, bid_qty, ask, ask_qty):
    book = OrderBook()
    book.apply_hl([[{"px": str(bid), "sz": str(bid_qty)}],
                   [{"px": str(ask), "sz": str(ask_qty)}]])
    return book


def test_trade_csv_records_execution_and_hedge_context(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
""")
    cfg = load_config(str(config_file), NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    cfg.trades_csv = str(tmp_path / "trades.csv")

    eng = Engine(cfg)
    buy = StubVenue("entropy", "ENTROPY", [
        {"status": "filled", "filled_base": 1.0, "avg_px": 100.1,
         "err": None, "unresolved": False},
        {"status": "filled", "filled_base": 0.25, "avg_px": 99.9,
         "err": None, "unresolved": False},
    ], delay=0.03)
    sell = StubVenue("hedge", "RH", [{
        "status": "filled", "filled_base": 0.75, "avg_px": 101.1,
        "err": None, "unresolved": False,
    }])
    buy.book = _book(99.9, 4.0, 100.0, 2.0)
    sell.book = _book(101.0, 10.0, 101.2, 10.0)
    eng.entropy = buy
    eng.hedge = sell
    eng.venues = {"entropy": buy, "hedge": sell}
    eng._step = 0.01
    eng._min_base = 0.1
    eng._min_notional = 10.0

    plan = ArbPlan(
        qty=1.0, buy_limit=100.0, sell_limit=101.0,
        buy_notional=100.0, sell_notional=101.0,
        q_max=1.0, q_max_notional=100.0,
        top_premium_bps=100.0, marginal_premium_bps=100.0,
        buy_fee=0.0, sell_fee=0.0,
    )

    async def run_execution():
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy, sell, plan)

    asyncio.run(run_execution())

    import csv
    with open(cfg.trades_csv, newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == CSV_HEADER
        row = next(reader)

    assert row["symbol"] == "SNDK"
    assert row["hedge"] == "lighter-rh"
    assert row["run_id"] == eng.run_id
    assert row["buy_venue"] == "ENTROPY"
    assert row["sell_venue"] == "RH"
    assert float(row["buy_bbo_px"]) == 100.0
    assert float(row["sell_bbo_px"]) == 101.0
    assert float(row["buy_bbo_qty"]) == 2.0
    assert float(row["sell_bbo_qty"]) == 10.0
    assert float(row["buy_avg_px"]) == 100.1
    assert float(row["sell_avg_px"]) == 101.1
    assert float(row["buy_protect_limit"]) == 100.5
    assert float(row["sell_protect_limit"]) == 100.495
    assert float(row["matched_qty"]) == 0.75
    assert float(row["residual_qty"]) == 0.25
    assert row["unresolved"] == "0"
    assert row["ok"] == "1"
    assert float(row["hedge_fill"]) == 0.25
    assert row["hedge_status"] == "filled"
    assert row["hedge_venue"] == "ENTROPY"
    assert row["hedge_side"] == "sell"
    assert float(row["hedge_avg_px"]) == 99.9
    assert float(row["hedge_notional"]) == pytest.approx(24.975)
    assert float(row["remaining_net_qty"]) == 0.0
    assert float(row["execution_ms"]) >= 0.0
    assert float(row["leg_settle_gap_ms"]) >= 20.0
    assert float(row["buy_settle_ms"]) >= 20.0
    assert float(row["sell_settle_ms"]) >= 0.0
    assert row["first_settled_leg"] == "sell"
    assert row["buy_reason"] == ""
    assert row["sell_reason"] == ""
    assert float(row["hedge_duration_ms"]) >= 0.0
