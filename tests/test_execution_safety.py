"""Deterministic generic two-leg execution safety contract tests."""
import asyncio
import os
import tempfile

import pytest

from entropy_arb.book import ArbPlan, OrderBook
from entropy_arb.config import load_config
from entropy_arb.engine import Engine


NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg():
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("""
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
execution:
  leg_slippage_bps: 1.0
  max_consecutive_errors: 99
""")
    f.close()
    cfg = load_config(f.name, NO_ENV, symbol="SNDK",
                      venue_a="entropy", venue_b="lighter-rh")
    cfg.trades_csv = tempfile.NamedTemporaryFile(
        suffix=".csv", delete=True).name
    return cfg


def outcome(status, filled, avg_px=None, err=None, unresolved=False):
    return {"status": status, "filled_base": filled, "avg_px": avg_px,
            "err": err, "unresolved": unresolved}


class ScriptedVenue:
    """Small fake with one deterministic result per send_taker call."""

    def __init__(self, key, name, scripted, call_log):
        self.key = key
        self.name = name
        self.kind = "fake"
        self.scripted = list(scripted)
        self.call_log = call_log
        self.calls = []
        self.book = OrderBook()
        self.book.apply_hl([[{"px": "100", "sz": "10"}],
                            [{"px": "101", "sz": "10"}]])
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0
        self.cap_usd = 10_000.0
        self.orders_per_min = 120
        self.min_base = 0.0001
        self.min_quote = 10.0
        self.size_decimals = 4
        self.fee_source = "configured"
        self.effective_taker_fee_bps = 2.0
        self.fee_bps = 2.0
        self.last_traded_ts = 0.0

    def px_round(self, price, round_up):
        return price

    async def send_taker(self, *, is_buy, qty, limit_px, reduce_only=False):
        call = {"venue": self.name, "is_buy": is_buy, "qty": qty,
                "limit_px": limit_px, "reduce_only": reduce_only}
        self.calls.append(call)
        self.call_log.append(call)
        if not self.scripted:
            raise AssertionError(f"unexpected extra send on {self.name}")
        result = self.scripted.pop(0)
        if isinstance(result, BaseException):
            raise result
        return dict(result)


def make_engine(buy_script, sell_script, *, direction="buy_a_sell_b"):
    cfg = make_cfg()
    call_log = []
    a = ScriptedVenue("venue_a", "A", [], call_log)
    b = ScriptedVenue("venue_b", "B", [], call_log)
    eng = Engine(cfg)
    eng.venue_a = a
    eng.venue_b = b
    eng.venues = {"venue_a": a, "venue_b": b}
    eng._step = 0.0001
    eng._min_base = 0.0001
    eng._min_notional = 10.0
    if direction == "buy_a_sell_b":
        buy, sell = a, b
        buy.scripted = list(buy_script)
        sell.scripted = list(sell_script)
    else:
        buy, sell = b, a
        buy.scripted = list(buy_script)
        sell.scripted = list(sell_script)
    return eng, buy, sell, call_log


def make_plan(*, buy_fee=2.0, sell_fee=4.5, qty=1.0):
    return ArbPlan(
        qty=qty, buy_limit=100.0, sell_limit=101.0,
        buy_notional=100.0 * qty, sell_notional=101.0 * qty,
        q_max=qty, q_max_notional=100.0 * qty,
        top_premium_bps=100.0, marginal_premium_bps=100.0,
        buy_fee=buy_fee / 1e4, sell_fee=sell_fee / 1e4)


def run_execution(eng, buy, sell, plan=None):
    return asyncio.run(eng._execute(buy, sell, plan or make_plan()))


def test_full_first_and_second_legs_complete_pair():
    eng, buy, sell, calls = make_engine(
        [outcome("filled", 1.0, 100.0)],
        [outcome("filled", 1.0, 101.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "PAIR_COMPLETED"
    assert result.requested_base == 1.0
    assert result.leg1_filled_base == 1.0
    assert result.leg2_filled_base == 1.0
    assert result.matched_base == result.completed_base == 1.0
    assert result.residual_base == 0.0
    assert result.final_known_residual == 0.0
    assert calls[0]["venue"] == "A"
    assert calls[1]["venue"] == "B"


def test_partial_first_leg_drives_partial_hedge_quantity():
    eng, buy, sell, calls = make_engine(
        [outcome("partially-filled", 0.4, 100.0)],
        [outcome("filled", 0.4, 101.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "PAIR_COMPLETED"
    assert result.leg1_filled_base == 0.4
    assert result.leg2_filled_base == 0.4
    assert result.matched_base == 0.4
    assert calls[0]["qty"] == 1.0
    assert calls[1]["qty"] == 0.4
    assert buy.position == pytest.approx(0.4)
    assert sell.position == pytest.approx(-0.4)


@pytest.mark.parametrize("direction", ["buy_a_sell_b", "sell_a_buy_b"])
def test_direction_symmetry_uses_the_same_state_machine(direction):
    eng, buy, sell, calls = make_engine(
        [outcome("partially-filled", 0.4, 100.0)],
        [outcome("filled", 0.4, 101.0)], direction=direction)
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "PAIR_COMPLETED"
    assert [call["venue"] for call in calls] == [buy.name, sell.name]
    assert [call["qty"] for call in calls] == [1.0, 0.4]


@pytest.mark.parametrize("direction", ["buy_a_sell_b", "sell_a_buy_b"])
def test_emergency_unwind_is_symmetric_for_both_directions(direction):
    eng, buy, sell, calls = make_engine(
        [outcome("filled", 1.0, 100.0), outcome("filled", 0.4, 99.0)],
        [outcome("partially-filled", 0.6, 101.0)], direction=direction)
    result = run_execution(eng, buy, sell)

    assert result.status == "PAIR_COMPLETED_AFTER_UNWIND"
    assert [call["venue"] for call in calls] == [buy.name, sell.name, buy.name]
    assert [call["qty"] for call in calls] == [1.0, 1.0, 0.4]
    assert calls[2]["is_buy"] is False
    assert calls[2]["reduce_only"] is True
    assert result.final_known_residual == 0.0


def test_known_zero_first_leg_submits_no_second_leg():
    eng, buy, sell, calls = make_engine(
        [outcome("canceled", 0.0)],
        [outcome("filled", 1.0, 101.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "FIRST_LEG_ZERO_FILL"
    assert result.matched_base == 0.0
    assert [call["venue"] for call in calls] == [buy.name]
    assert len(sell.calls) == 0


def test_rejected_first_leg_submits_no_second_leg():
    eng, buy, sell, calls = make_engine(
        [outcome("rejected", 0.0, err="rejected")],
        [outcome("filled", 1.0, 101.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "FIRST_LEG_REJECTED"
    assert result.unresolved is False
    assert len(calls) == 1


def test_unresolved_first_leg_submits_no_hedge_or_unwind():
    eng, buy, sell, calls = make_engine(
        [outcome("timeout", 0.0, unresolved=True)],
        [outcome("filled", 1.0, 101.0)], direction="sell_a_buy_b")
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "UNRESOLVED_FIRST_LEG"
    assert result.unresolved is True
    assert result.final_known_residual is None
    assert [call["venue"] for call in calls] == [buy.name]


def test_first_leg_exception_is_unknown_and_submits_no_hedge():
    eng, buy, sell, calls = make_engine(
        [TimeoutError("request timed out")],
        [outcome("filled", 1.0, 101.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "UNRESOLVED_FIRST_LEG"
    assert result.unresolved is True
    assert len(calls) == 1


def test_known_second_leg_zero_triggers_one_full_residual_unwind():
    eng, buy, sell, calls = make_engine(
        [outcome("filled", 1.0, 100.0), outcome("filled", 1.0, 99.0)],
        [outcome("canceled", 0.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "PAIR_COMPLETED_AFTER_UNWIND"
    assert result.emergency_unwind is True
    assert result.residual_base == 1.0
    assert result.unwind_filled_base == 1.0
    assert result.final_known_residual == 0.0
    assert [call["qty"] for call in calls] == [1.0, 1.0, 1.0]
    assert calls[2]["venue"] == buy.name
    assert calls[2]["is_buy"] is False
    assert calls[2]["reduce_only"] is True
    assert buy.position == pytest.approx(0.0)
    assert sell.position == pytest.approx(0.0)


def test_second_leg_partial_unwinds_only_known_residual():
    eng, buy, sell, calls = make_engine(
        [outcome("filled", 1.0, 100.0), outcome("filled", 0.4, 99.0)],
        [outcome("partially-filled", 0.6, 101.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "PAIR_COMPLETED_AFTER_UNWIND"
    assert result.leg1_filled_base == 1.0
    assert result.leg2_filled_base == 0.6
    assert result.matched_base == 0.6
    assert result.residual_base == pytest.approx(0.4)
    assert result.unwind_filled_base == pytest.approx(0.4)
    assert result.final_known_residual == 0.0
    assert calls[2]["qty"] == pytest.approx(0.4)
    assert buy.position == pytest.approx(0.6)
    assert sell.position == pytest.approx(-0.6)


def test_partial_first_leg_zero_second_leg_unwinds_actual_partial_fill():
    eng, buy, sell, calls = make_engine(
        [outcome("partially-filled", 0.4, 100.0), outcome("filled", 0.4, 99.0)],
        [outcome("rejected", 0.0, err="rejected")])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "PAIR_COMPLETED_AFTER_UNWIND"
    assert result.residual_base == pytest.approx(0.4)
    assert result.unwind_filled_base == pytest.approx(0.4)
    assert [call["qty"] for call in calls] == [1.0, 0.4, 0.4]
    assert buy.position == pytest.approx(0.0)
    assert sell.position == pytest.approx(0.0)


def test_unresolved_second_leg_never_unwinds_first_leg():
    eng, buy, sell, calls = make_engine(
        [outcome("filled", 1.0, 100.0)],
        [outcome("timeout", 0.0, unresolved=True)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "UNRESOLVED_SECOND_LEG"
    assert result.unresolved is True
    assert result.final_known_residual is None
    assert len(calls) == 2
    assert buy.position == pytest.approx(1.0)
    assert sell.position == pytest.approx(0.0)


def test_partial_unwind_reports_known_remaining_residual_without_retry():
    eng, buy, sell, calls = make_engine(
        [outcome("filled", 1.0, 100.0), outcome("partially-filled", 0.3, 99.0)],
        [outcome("filled", 0.6, 101.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "RESIDUAL_EXPOSURE"
    assert result.unresolved is False
    assert result.residual_base == pytest.approx(0.4)
    assert result.unwind_filled_base == pytest.approx(0.3)
    assert result.final_known_residual == pytest.approx(0.1)
    assert len(calls) == 3
    assert buy.position == pytest.approx(0.7)
    assert sell.position == pytest.approx(-0.6)


def test_zero_fill_unwind_reports_full_known_residual_without_retry():
    eng, buy, sell, calls = make_engine(
        [outcome("filled", 1.0, 100.0), outcome("canceled", 0.0)],
        [outcome("canceled", 0.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "RESIDUAL_EXPOSURE"
    assert result.final_known_residual == pytest.approx(1.0)
    assert len(calls) == 3
    assert buy.position == pytest.approx(1.0)


def test_unresolved_unwind_requires_reconciliation_without_more_orders():
    eng, buy, sell, calls = make_engine(
        [outcome("filled", 1.0, 100.0),
         outcome("timeout", 0.0, unresolved=True)],
        [outcome("canceled", 0.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "UNRESOLVED_UNWIND"
    assert result.unresolved is True
    assert result.residual_base == pytest.approx(1.0)
    assert result.final_known_residual is None
    assert len(calls) == 3


def test_emergency_unwind_is_never_retried():
    eng, buy, sell, calls = make_engine(
        [outcome("filled", 1.0, 100.0), outcome("canceled", 0.0)],
        [outcome("canceled", 0.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "RESIDUAL_EXPOSURE"
    assert len(buy.calls) == 2
    assert len(sell.calls) == 1
    assert len(calls) == 3


def test_no_execution_path_can_submit_a_fourth_order():
    eng, buy, sell, calls = make_engine(
        [outcome("filled", 1.0, 100.0), outcome("filled", 1.0, 99.0)],
        [outcome("canceled", 0.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "PAIR_COMPLETED_AFTER_UNWIND"
    assert len(calls) == 3
    assert len(buy.scripted) == 0
    assert len(sell.scripted) == 0


def test_negative_first_leg_fill_is_rejected_without_hedge():
    eng, buy, sell, calls = make_engine(
        [outcome("filled", -0.1)],
        [outcome("filled", 1.0, 101.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "INVALID_FIRST_LEG_RESULT"
    assert result.unresolved is True
    assert len(calls) == 1
    assert buy.position == pytest.approx(0.0)


def test_overreported_second_leg_is_rejected_without_unwind():
    eng, buy, sell, calls = make_engine(
        [outcome("filled", 1.0, 100.0)],
        [outcome("filled", 1.1, 101.0)])
    result = run_execution(eng, buy, sell)

    assert getattr(result, "status", None) == "INVALID_SECOND_LEG_RESULT"
    assert result.unresolved is True
    assert len(calls) == 2
    assert buy.position == pytest.approx(1.0)
    assert sell.position == pytest.approx(0.0)


def test_effective_fees_are_applied_to_actual_fill_accounting():
    eng, buy, sell, _ = make_engine(
        [outcome("partially-filled", 0.4, 100.0)],
        [outcome("filled", 0.4, 101.0)])
    plan = make_plan(buy_fee=2.0, sell_fee=4.5)
    result = run_execution(eng, buy, sell, plan)

    assert getattr(result, "status", None) == "PAIR_COMPLETED"
    assert buy.cash == pytest.approx(-40.0 * (1.0 + 0.0002))
    assert sell.cash == pytest.approx(40.4 * (1.0 - 0.00045))
    assert buy.volume_usd == pytest.approx(40.0)
    assert sell.volume_usd == pytest.approx(40.4)
