"""Offline comparison of enabled/disabled execution journal on original Engine.

No exchange credentials or network calls are needed.
"""
import asyncio
import json

from entropy_arb.engine import Engine
from entropy_arb.config import load_config
from entropy_arb.book import OrderBook, plan_arb


class FakeVenue:
    def __init__(self, key, filled_ratio=1.0, unresolved=False):
        self.key = key
        self.name = key
        self.position = self.cash = self.volume_usd = 0.0
        self.fee_bps = 0.0
        self.last_traded_ts = 0.0
        self.filled_ratio = filled_ratio
        self.unresolved = unresolved
        self.book = OrderBook()
        self.book.apply_hl([[{"px": "100", "sz": "10"}],
                            [{"px": "101", "sz": "10"}]])

    def px_round(self, px, round_up=False):
        return px

    async def send_taker(self, *, is_buy, qty, limit_px, trace=None):
        if trace is not None:
            from entropy_arb.execution_journal import stamp
            trace["test_client_order_id"] = self.key
            stamp(trace, "submit_enter")
            stamp(trace, "request_start")
            stamp(trace, "api_response")
            if not self.unresolved:
                stamp(trace, "settlement_observed")
        fill = 0.0 if self.unresolved else qty * self.filled_ratio
        return {"status": "timeout" if self.unresolved else "filled",
                "filled_base": fill, "avg_px": None if self.unresolved else limit_px,
                "err": None, "unresolved": self.unresolved}


async def exercise(tmp_path, enabled, buy_ratio=1, sell_ratio=1, timeout=False):
    cfg = load_config(str(tmp_path / "config.yaml"),
                      str(tmp_path / "missing.env"),
                      symbol="SNDK", hedge_venue="lighter-rh")
    cfg.journal_enabled = enabled
    cfg.journal_path = str(tmp_path / ("on.jsonl" if enabled else "off.jsonl"))
    cfg.trades_csv = str(tmp_path / ("trades-on.csv" if enabled else "trades-off.csv"))
    cfg.inventory_scale_bps = 0
    engine = Engine(cfg)
    buy = FakeVenue("hedge", buy_ratio, unresolved=timeout)
    sell = FakeVenue("entropy", sell_ratio)
    engine.entropy = sell
    engine.hedge = buy
    engine.venues = {"entropy": sell, "hedge": buy}
    engine.journal.start()
    plan, why = plan_arb(buy.book, sell.book, threshold_bps=-200,
                         buy_fee_bps=0, sell_fee_bps=0, take_fraction=0.5,
                         cap_notional=200, min_base=0.001,
                         min_notional=10, size_step=0.001)
    # With identical books, a negative threshold permits a legitimate planner
    # decision (only for the offline execution-path test).
    assert why == "ok"
    result = await engine._execute(buy, sell, plan)
    await engine.journal.close()
    events = []
    if enabled:
        events = [json.loads(line) for line in
                  (tmp_path / "on.jsonl").read_text().splitlines()]
    outcome = (result, buy.position, sell.position, buy.cash, sell.cash,
               engine.trades, engine.total_fill_edge)
    return outcome, events


def test_enabled_vs_disabled_same_execution(tmp_path):
    (tmp_path / "config.yaml").write_text("thresholds:\n  midline_bps: 0\n  upper_bps: 4\n  lower_bps: 4\n")
    off, _ = asyncio.run(exercise(tmp_path, False))
    on, events = asyncio.run(exercise(tmp_path, True))
    assert on == off
    assert [x["event_type"] for x in events] == [
        "decision", "order_submit", "order_submit",
        "order_result", "order_result", "execution_complete"]
    assert len({e["execution_id"] for e in events}) == 1
    assert events[-1]["unresolved"] is False


def test_partial_fill_retains_residual(tmp_path):
    (tmp_path / "config.yaml").write_text("thresholds:\n  midline_bps: 0\n  upper_bps: 4\n  lower_bps: 4\n")
    outcome, events = asyncio.run(exercise(tmp_path, True, buy_ratio=0.5))
    assert outcome[1] > 0
    assert outcome[2] < 0
    assert events[-1]["residual_base"] < 0
    assert len([e for e in events if e["event_type"] == "order_result"]) == 2


def test_unresolved_result_not_assumed_zero_fill(tmp_path):
    (tmp_path / "config.yaml").write_text("thresholds:\n  midline_bps: 0\n  upper_bps: 4\n  lower_bps: 4\n")
    outcome, events = asyncio.run(exercise(tmp_path, True, timeout=True))
    assert outcome[0] is True
    assert events[-1]["unresolved"] is True
    assert any(e["event_type"] == "order_result" and e["unresolved"]
               for e in events)
