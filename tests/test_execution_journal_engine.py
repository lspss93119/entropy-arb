"""Offline comparison of enabled/disabled execution journal on original Engine.

No exchange credentials or network calls are needed.
"""
import asyncio
import json

from entropy_arb.engine import Engine
from entropy_arb.config import load_config
from entropy_arb.book import OrderBook, plan_arb


class FakeVenue:
    def __init__(self, key, filled_ratio=1.0, unresolved=False,
                 reject=False, raise_error=False):
        self.key = key
        self.name = key
        self.position = self.cash = self.volume_usd = 0.0
        self.fee_bps = 0.0
        self.min_base = 0.001
        self.min_quote = 10.0
        self.cap_usd = 1000.0
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.filled_ratio = filled_ratio
        self.unresolved = unresolved
        self.reject = reject
        self.raise_error = raise_error
        self.calls = []
        self.book = OrderBook()
        self.book.apply_hl([[{"px": "100", "sz": "10"}],
                            [{"px": "101", "sz": "10"}]])

    def px_round(self, px, round_up=False):
        return px

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False,
                         trace=None):
        self.calls.append({"is_buy": is_buy, "qty": qty,
                           "limit_px": limit_px, "reduce_only": reduce_only})
        if trace is not None:
            from entropy_arb.execution_journal import stamp
            trace["test_client_order_id"] = self.key
            stamp(trace, "submit_enter")
            stamp(trace, "request_start")
            stamp(trace, "api_response")
            if not self.unresolved:
                stamp(trace, "settlement_observed")
        if self.raise_error:
            raise ConnectionError(f"{self.key} websocket disconnected")
        if self.reject:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": "API rejected", "unresolved": False,
                    "exchange_order_id": None}
        fill = 0.0 if self.unresolved else qty * self.filled_ratio
        return {"status": "timeout" if self.unresolved else "filled",
                "filled_base": fill, "avg_px": None if self.unresolved else limit_px,
                "err": None, "unresolved": self.unresolved,
                "exchange_order_id": f"{self.key}-exchange-order"}


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


def test_auto_hedge_is_correlated_and_reduce_only(tmp_path):
    async def scenario():
        (tmp_path / "config.yaml").write_text(
            "thresholds:\n  midline_bps: 0\n  upper_bps: 4\n  lower_bps: 4\n")
        cfg = load_config(str(tmp_path / "config.yaml"),
                          str(tmp_path / "missing.env"),
                          symbol="SNDK", hedge_venue="lighter-rh")
        cfg.journal_enabled = True
        cfg.journal_path = str(tmp_path / "hedge.jsonl")
        cfg.trades_csv = str(tmp_path / "trades.csv")
        cfg.inventory_scale_bps = 0
        engine = Engine(cfg)
        buy = FakeVenue("hedge", filled_ratio=0.5)
        sell = FakeVenue("entropy")
        engine.entropy = sell
        engine.hedge = buy
        engine.venues = {"entropy": sell, "hedge": buy}
        engine.journal.start()
        plan, why = plan_arb(buy.book, sell.book, threshold_bps=-200,
                             buy_fee_bps=0, sell_fee_bps=0, take_fraction=0.5,
                             cap_notional=200, min_base=0.001,
                             min_notional=10, size_step=0.001)
        assert why == "ok"
        await engine._vlock(buy.key).acquire()
        await engine._vlock(sell.key).acquire()
        await engine._execute_locked(buy, sell, plan)
        await engine.journal.close()
        return [json.loads(line) for line in
                (tmp_path / "hedge.jsonl").read_text().splitlines()]

    events = asyncio.run(scenario())
    hedge_events = [e for e in events if e["event_type"].startswith("hedge_")]
    assert [e["event_type"] for e in hedge_events] == [
        "hedge_submit", "hedge_result"]
    execution_ids = {e["execution_id"] for e in events
                     if e["event_type"] == "decision"}
    assert len(execution_ids) == 1
    assert {e["execution_id"] for e in hedge_events} == execution_ids
    assert hedge_events[0]["leg_id"] == "hedge:entropy"
    assert hedge_events[0]["reduce_only"] is True
    assert hedge_events[1]["exchange_order_id"] == "entropy-exchange-order"


def test_standalone_hedge_is_journaled(tmp_path):
    async def scenario():
        (tmp_path / "config.yaml").write_text(
            "thresholds:\n  midline_bps: 0\n  upper_bps: 4\n  lower_bps: 4\n")
        cfg = load_config(str(tmp_path / "config.yaml"),
                          str(tmp_path / "missing.env"),
                          symbol="SNDK", hedge_venue="lighter-rh")
        cfg.journal_enabled = True
        cfg.journal_path = str(tmp_path / "reconcile-hedge.jsonl")
        cfg.trades_csv = str(tmp_path / "trades.csv")
        engine = Engine(cfg)
        buy = FakeVenue("hedge")
        sell = FakeVenue("entropy")
        buy.position = 0.5
        engine.entropy = sell
        engine.hedge = buy
        engine.venues = {"entropy": sell, "hedge": buy}
        engine.journal.start()
        await engine._maybe_hedge()
        await engine.journal.close()
        return [json.loads(line) for line in
                (tmp_path / "reconcile-hedge.jsonl").read_text().splitlines()]

    events = asyncio.run(scenario())
    hedge_events = [e for e in events if e["event_type"].startswith("hedge_")]
    assert [e["event_type"] for e in hedge_events] == [
        "hedge_submit", "hedge_result"]
    assert len({e["execution_id"] for e in hedge_events}) == 1


def test_reconcile_hedge_keeps_unresolved_execution_id(tmp_path):
    async def scenario():
        (tmp_path / "config.yaml").write_text(
            "thresholds:\n  midline_bps: 0\n  upper_bps: 4\n  lower_bps: 4\n")
        cfg = load_config(str(tmp_path / "config.yaml"),
                          str(tmp_path / "missing.env"),
                          symbol="SNDK", hedge_venue="lighter-rh")
        cfg.journal_enabled = True
        cfg.journal_path = str(tmp_path / "reconcile.jsonl")
        cfg.trades_csv = str(tmp_path / "trades.csv")
        engine = Engine(cfg)
        buy = FakeVenue("hedge")
        sell = FakeVenue("entropy")
        buy.position = 0.5
        async def fetch_position(v):
            return v.position
        buy.fetch_position = lambda: fetch_position(buy)
        sell.fetch_position = lambda: fetch_position(sell)
        engine.entropy = sell
        engine.hedge = buy
        engine.venues = {"entropy": sell, "hedge": buy}
        engine._reconcile_execution_id = "unresolved-e1"
        engine.journal.start()
        await engine._reconcile_positions(hedge=True)
        await engine.journal.close()
        events = [json.loads(line) for line in
                  (tmp_path / "reconcile.jsonl").read_text().splitlines()]
        return events, engine._reconcile_execution_id

    events, pending_id = asyncio.run(scenario())
    hedge_events = [e for e in events if e["event_type"].startswith("hedge_")]
    assert {e["execution_id"] for e in hedge_events} == {"unresolved-e1"}
    assert pending_id is None


def test_unexpected_execute_exception_is_journaled_and_reconciled(tmp_path):
    async def scenario():
        (tmp_path / "config.yaml").write_text(
            "thresholds:\n  midline_bps: 0\n  upper_bps: 4\n  lower_bps: 4\n")
        cfg = load_config(str(tmp_path / "config.yaml"),
                          str(tmp_path / "missing.env"),
                          symbol="SNDK", hedge_venue="lighter-rh")
        cfg.journal_enabled = True
        cfg.journal_path = str(tmp_path / "execute-error.jsonl")
        cfg.trades_csv = str(tmp_path / "trades.csv")
        engine = Engine(cfg)
        buy = FakeVenue("hedge")
        sell = FakeVenue("entropy")
        engine.entropy = sell
        engine.hedge = buy
        engine.venues = {"entropy": sell, "hedge": buy}
        plan, why = plan_arb(buy.book, sell.book, threshold_bps=-200,
                             buy_fee_bps=0, sell_fee_bps=0, take_fraction=0.5,
                             cap_notional=200, min_base=0.001,
                             min_notional=10, size_step=0.001)
        assert why == "ok"

        async def explode(*args, **kwargs):
            raise RuntimeError("adapter failed")

        engine._execute = explode
        engine.journal.start()
        await engine._vlock(buy.key).acquire()
        await engine._vlock(sell.key).acquire()
        await engine._execute_locked(buy, sell, plan)
        await engine.journal.close()
        events = [json.loads(line) for line in
                  (tmp_path / "execute-error.jsonl").read_text().splitlines()]
        return events, engine._reconcile_execution_id, buy.key, sell.key

    events, pending_id, buy_key, sell_key = asyncio.run(scenario())
    assert len(events) == 1
    assert events[0]["event_type"] == "execution_error"
    assert events[0]["unresolved"] is True
    assert events[0]["success"] is False
    assert events[0]["buy_venue"] == buy_key
    assert events[0]["sell_venue"] == sell_key
    assert pending_id == events[0]["execution_id"]


def test_unhandled_send_exception_is_unresolved(tmp_path):
    async def scenario():
        (tmp_path / "config.yaml").write_text(
            "thresholds:\n  midline_bps: 0\n  upper_bps: 4\n  lower_bps: 4\n")
        cfg = load_config(str(tmp_path / "config.yaml"),
                          str(tmp_path / "missing.env"),
                          symbol="SNDK", hedge_venue="lighter-rh")
        cfg.journal_enabled = True
        cfg.journal_path = str(tmp_path / "unknown.jsonl")
        cfg.trades_csv = str(tmp_path / "trades.csv")
        cfg.inventory_scale_bps = 0
        engine = Engine(cfg)
        buy = FakeVenue("hedge", raise_error=True)
        sell = FakeVenue("entropy")
        engine.entropy = sell
        engine.hedge = buy
        engine.venues = {"entropy": sell, "hedge": buy}
        engine.journal.start()
        plan, why = plan_arb(buy.book, sell.book, threshold_bps=-200,
                             buy_fee_bps=0, sell_fee_bps=0, take_fraction=0.5,
                             cap_notional=200, min_base=0.001,
                             min_notional=10, size_step=0.001)
        assert why == "ok"
        result = await engine._execute(buy, sell, plan)
        await engine.journal.close()
        events = [json.loads(line) for line in
                  (tmp_path / "unknown.jsonl").read_text().splitlines()]
        return result, events

    result, events = asyncio.run(scenario())
    assert result is True
    assert any(e["event_type"] == "order_result" and e["unresolved"]
               and e["status"] == "unknown-exception" for e in events)
    assert events[-1]["unresolved"] is True


def test_rejected_order_is_terminal_not_unknown(tmp_path):
    async def scenario():
        (tmp_path / "config.yaml").write_text(
            "thresholds:\n  midline_bps: 0\n  upper_bps: 4\n  lower_bps: 4\n")
        cfg = load_config(str(tmp_path / "config.yaml"),
                          str(tmp_path / "missing.env"),
                          symbol="SNDK", hedge_venue="lighter-rh")
        cfg.journal_enabled = True
        cfg.journal_path = str(tmp_path / "reject.jsonl")
        cfg.trades_csv = str(tmp_path / "trades.csv")
        cfg.inventory_scale_bps = 0
        engine = Engine(cfg)
        buy = FakeVenue("hedge", reject=True)
        sell = FakeVenue("entropy")
        engine.entropy = sell
        engine.hedge = buy
        engine.venues = {"entropy": sell, "hedge": buy}
        engine.journal.start()
        plan, why = plan_arb(buy.book, sell.book, threshold_bps=-200,
                             buy_fee_bps=0, sell_fee_bps=0, take_fraction=0.5,
                             cap_notional=200, min_base=0.001,
                             min_notional=10, size_step=0.001)
        assert why == "ok"
        result = await engine._execute(buy, sell, plan)
        await engine.journal.close()
        events = [json.loads(line) for line in
                  (tmp_path / "reject.jsonl").read_text().splitlines()]
        return result, events

    result, events = asyncio.run(scenario())
    assert result is False
    rejected = [e for e in events if e["event_type"] == "order_result"
                and e["leg_id"] == "hedge"]
    assert rejected[0]["unresolved"] is False
    assert rejected[0]["error_type"] == "present"


def test_journal_toggle_preserves_calls_positions_cash_and_hedges(tmp_path):
    async def scenario(enabled):
        cfg_path = tmp_path / ("on.yaml" if enabled else "off.yaml")
        cfg_path.write_text(
            "thresholds:\n  midline_bps: 0\n  upper_bps: 4\n  lower_bps: 4\n")
        cfg = load_config(str(cfg_path), str(tmp_path / "missing.env"),
                          symbol="SNDK", hedge_venue="lighter-rh")
        cfg.journal_enabled = enabled
        cfg.journal_path = str(tmp_path / ("on.jsonl" if enabled else "off.jsonl"))
        cfg.trades_csv = str(tmp_path / ("on.csv" if enabled else "off.csv"))
        cfg.inventory_scale_bps = 0
        engine = Engine(cfg)
        buy = FakeVenue("hedge", filled_ratio=0.5)
        sell = FakeVenue("entropy")
        engine.entropy = sell
        engine.hedge = buy
        engine.venues = {"entropy": sell, "hedge": buy}
        engine.journal.start()
        plan, why = plan_arb(buy.book, sell.book, threshold_bps=-200,
                             buy_fee_bps=0, sell_fee_bps=0, take_fraction=0.5,
                             cap_notional=200, min_base=0.001,
                             min_notional=10, size_step=0.001)
        assert why == "ok"
        await engine._vlock(buy.key).acquire()
        await engine._vlock(sell.key).acquire()
        await engine._execute_locked(buy, sell, plan)
        await engine.journal.close()
        return {
            "calls": buy.calls + sell.calls,
            "positions": (buy.position, sell.position),
            "cash": (buy.cash, sell.cash),
            "volume": (buy.volume_usd, sell.volume_usd),
            "trades_hedges": (engine.trades, engine.hedges),
        }

    off = asyncio.run(scenario(False))
    on = asyncio.run(scenario(True))
    assert on == off
