"""Rolling lot economics, persistence, and reduce-depth planning."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import (  # noqa: E402
    OrderBook,
    floor_step,
    plan_reduce_arb,
)
from entropy_arb.lot_ledger import LotLedger, LotLedgerError  # noqa: E402


def _book(bids, asks):
    book = OrderBook()
    book.apply_hl([[{"px": str(px), "sz": str(sz)} for px, sz in bids],
                   [{"px": str(px), "sz": str(sz)} for px, sz in asks]])
    return book


def _lot(ledger, lot_id, direction, qty, buy_px, sell_px,
         buy_fee_bps=10.0, sell_fee_bps=20.0):
    return ledger.add_lot(
        lot_id=lot_id,
        source_event_id=lot_id,
        direction=direction,
        open_qty=qty,
        entry_ts=100.0,
        buy_venue="ENTROPY" if direction == "buy_entropy" else "RH",
        sell_venue="RH" if direction == "buy_entropy" else "ENTROPY",
        buy_avg_px=buy_px,
        sell_avg_px=sell_px,
        buy_fee_bps=buy_fee_bps,
        sell_fee_bps=sell_fee_bps,
    )


def test_lot_stores_actual_entry_cashflow_and_reference():
    ledger = LotLedger()
    lot = _lot(ledger, "lot-1", "sell_entropy", 1.0, 100.0, 102.0)

    assert lot.entry_cash_per_base == pytest.approx(
        102.0 * 0.998 - 100.0 * 1.001)
    assert lot.reference_entry_notional_per_base == pytest.approx(101.0)
    assert lot.required_exit_cash_per_base(0.0) == pytest.approx(
        -lot.entry_cash_per_base)


def test_ledger_persists_atomically_and_reloads(tmp_path):
    path = tmp_path / "lots.json"
    ledger = LotLedger(str(path), symbol="SNDK", hedge="lighter-rh")
    _lot(ledger, "lot-1", "buy_entropy", 0.5, 100.0, 101.0)
    ledger.save()

    loaded = LotLedger(str(path), symbol="SNDK", hedge="lighter-rh")
    loaded.load()

    assert len(loaded.lots) == 1
    assert loaded.lots[0].lot_id == "lot-1"
    assert loaded.lots[0].open_qty == pytest.approx(0.5)


def test_corrupt_ledger_fails_closed(tmp_path):
    path = tmp_path / "lots.json"
    path.write_text("not-json")

    with pytest.raises(LotLedgerError, match="cannot load"):
        LotLedger(str(path)).load()


def test_close_allocations_decrement_only_actual_quantity():
    ledger = LotLedger()
    _lot(ledger, "lot-1", "sell_entropy", 1.0, 100.0, 102.0)
    _lot(ledger, "lot-2", "sell_entropy", 0.5, 99.0, 101.0)

    closed = ledger.close_allocations([
        {"lot_id": "lot-1", "qty": 0.4},
        {"lot_id": "lot-2", "qty": 0.3},
    ])

    assert closed == pytest.approx(0.7)
    assert ledger.total_qty == pytest.approx(0.8)
    assert ledger.lots[0].open_qty == pytest.approx(0.6)
    assert ledger.lots[1].open_qty == pytest.approx(0.2)


def test_position_validation_requires_direction_and_quantity_match():
    ledger = LotLedger(tolerance=1e-6)
    _lot(ledger, "lot-1", "sell_entropy", 1.0, 100.0, 102.0)

    ledger.validate_positions({"entropy": -1.0, "hedge": 1.0})
    with pytest.raises(LotLedgerError, match="position mismatch"):
        ledger.validate_positions({"entropy": -0.5, "hedge": 0.5})
    with pytest.raises(LotLedgerError, match="direction"):
        ledger.validate_positions({"entropy": 1.0, "hedge": -1.0})


def test_reduce_planner_selects_best_capture_and_stops_at_bad_depth():
    ledger = LotLedger()
    first = _lot(ledger, "lot-1", "sell_entropy", 0.5, 100.0, 102.0)
    second = _lot(ledger, "lot-2", "sell_entropy", 0.5, 100.0, 101.0)
    buy = _book([], [(100.0, 0.5), (101.0, 0.5)])
    sell = _book([(102.0, 0.5), (100.0, 0.5)], [])

    plan, reason = plan_reduce_arb(
        buy, sell, candidates=ledger.exit_candidates(0.0),
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=1.0,
        cap_notional=1_000.0, min_base=0.1, min_notional=10.0,
        size_step=0.1)

    assert reason == "ok"
    assert plan.reduce_only
    assert plan.qty == pytest.approx(0.5)
    assert plan.buy_limit == pytest.approx(100.0)
    assert plan.sell_limit == pytest.approx(102.0)
    assert plan.lot_allocations == ({"lot_id": first.lot_id, "qty": 0.5},)
    assert second.lot_id not in plan.selected_lot_ids


def test_reduce_planner_handles_multiple_lots_at_one_safe_level():
    ledger = LotLedger()
    _lot(ledger, "lot-1", "buy_entropy", 0.3, 100.0, 101.0)
    _lot(ledger, "lot-2", "buy_entropy", 0.4, 100.0, 101.0)
    buy = _book([], [(100.0, 1.0)])
    sell = _book([(102.0, 1.0)], [])

    plan, reason = plan_reduce_arb(
        buy, sell, candidates=ledger.exit_candidates(0.0),
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=1.0,
        cap_notional=1_000.0, min_base=0.1, min_notional=10.0,
        size_step=0.1)

    assert reason == "ok"
    assert plan.qty == pytest.approx(0.7)
    assert sum(item["qty"] for item in plan.lot_allocations) == pytest.approx(0.7)


def test_reduce_planner_applies_minimum_capture_floor():
    ledger = LotLedger()
    _lot(ledger, "lot-1", "sell_entropy", 0.5, 100.0, 101.0)
    buy = _book([], [(100.0, 0.5)])
    sell = _book([(101.0, 0.5)], [])

    plan, reason = plan_reduce_arb(
        buy, sell, candidates=ledger.exit_candidates(0.0),
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=1.0,
        cap_notional=1_000.0, min_base=0.1, min_notional=10.0,
        size_step=0.1)
    assert reason == "ok"
    assert plan is not None

    plan, reason = plan_reduce_arb(
        buy, sell, candidates=ledger.exit_candidates(200.0),
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=1.0,
        cap_notional=1_000.0, min_base=0.1, min_notional=10.0,
        size_step=0.1)
    assert plan is None
    assert reason == "no_break_even_depth"


def test_reduce_planner_rejects_losing_depth():
    ledger = LotLedger()
    _lot(ledger, "lot-1", "buy_entropy", 0.5, 100.0, 101.0)
    buy = _book([], [(100.5, 0.5)])
    sell = _book([(99.0, 0.5)], [])

    plan, reason = plan_reduce_arb(
        buy, sell, candidates=ledger.exit_candidates(0.0),
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=1.0,
        cap_notional=1_000.0, min_base=0.1, min_notional=10.0,
        size_step=0.1)

    assert plan is None
    assert reason == "no_break_even_depth"


def test_reduce_planner_closes_be_safe_terminal_remainder():
    ledger = LotLedger()
    _lot(ledger, "lot-1", "sell_entropy", 0.001, 2_000.0, 2_000.0,
         buy_fee_bps=0.0, sell_fee_bps=0.0)
    _lot(ledger, "lot-2", "sell_entropy", 0.014, 2_000.0, 2_000.0,
         buy_fee_bps=0.0, sell_fee_bps=0.0)
    buy = _book([], [(2_000.0, 0.015)])
    sell = _book([(2_000.0, 0.015)], [])

    assert floor_step(0.015 * 0.25, 0.001) == pytest.approx(0.003)
    plan, reason = plan_reduce_arb(
        buy, sell, candidates=ledger.exit_candidates(0.0),
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=0.25,
        cap_notional=1_000.0, min_base=0.0032, min_notional=10.0,
        size_step=0.001)

    assert reason == "ok"
    assert plan.qty == pytest.approx(0.015)
    assert sum(item["qty"] for item in plan.lot_allocations) == pytest.approx(
        0.015)


def test_reduce_planner_does_not_bypass_missing_terminal_be_depth():
    ledger = LotLedger()
    _lot(ledger, "lot-1", "sell_entropy", 0.015, 2_000.0, 2_000.0,
         buy_fee_bps=0.0, sell_fee_bps=0.0)
    buy = _book([], [(2_000.0, 0.010)])
    sell = _book([(2_000.0, 0.010)], [])

    plan, reason = plan_reduce_arb(
        buy, sell, candidates=ledger.exit_candidates(0.0),
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=0.25,
        cap_notional=1_000.0, min_base=0.0032, min_notional=10.0,
        size_step=0.001)

    assert plan is None
    assert reason == "below_min_base"


def test_reduce_planner_keeps_fractional_sizing_when_remainder_is_larger():
    ledger = LotLedger()
    _lot(ledger, "lot-1", "sell_entropy", 0.100, 2_000.0, 2_000.0,
         buy_fee_bps=0.0, sell_fee_bps=0.0)
    buy = _book([], [(2_000.0, 0.080)])
    sell = _book([(2_000.0, 0.080)], [])

    plan, reason = plan_reduce_arb(
        buy, sell, candidates=ledger.exit_candidates(0.0),
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=0.25,
        cap_notional=1_000.0, min_base=0.0032, min_notional=10.0,
        size_step=0.001)

    assert reason == "ok"
    assert plan.qty == pytest.approx(0.020)
    assert plan.q_max == pytest.approx(0.080)


def test_reduce_planner_keeps_true_dust_blocked():
    ledger = LotLedger()
    _lot(ledger, "lot-1", "sell_entropy", 0.003, 2_000.0, 2_000.0,
         buy_fee_bps=0.0, sell_fee_bps=0.0)
    buy = _book([], [(2_000.0, 0.003)])
    sell = _book([(2_000.0, 0.003)], [])

    plan, reason = plan_reduce_arb(
        buy, sell, candidates=ledger.exit_candidates(0.0),
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=0.25,
        cap_notional=1_000.0, min_base=0.0032, min_notional=1.0,
        size_step=0.001)

    assert plan is None
    assert reason == "below_min_base"


def test_reduce_planner_rejects_non_step_aligned_terminal_remainder():
    ledger = LotLedger()
    _lot(ledger, "lot-1", "sell_entropy", 0.0155, 2_000.0, 2_000.0,
         buy_fee_bps=0.0, sell_fee_bps=0.0)
    buy = _book([], [(2_000.0, 0.0155)])
    sell = _book([(2_000.0, 0.0155)], [])

    plan, reason = plan_reduce_arb(
        buy, sell, candidates=ledger.exit_candidates(0.0),
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=0.25,
        cap_notional=1_000.0, min_base=0.0032, min_notional=10.0,
        size_step=0.001)

    assert plan is None
    assert reason == "below_min_base"


def test_reduce_planner_terminal_fallback_still_requires_min_notional():
    ledger = LotLedger()
    _lot(ledger, "lot-1", "sell_entropy", 0.015, 100.0, 100.0,
         buy_fee_bps=0.0, sell_fee_bps=0.0)
    buy = _book([], [(100.0, 0.015)])
    sell = _book([(100.0, 0.015)], [])

    plan, reason = plan_reduce_arb(
        buy, sell, candidates=ledger.exit_candidates(0.0),
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=0.25,
        cap_notional=1_000.0, min_base=0.0032, min_notional=10.0,
        size_step=0.001)

    assert plan is None
    assert reason == "below_min_notional"


def test_ledger_json_has_versioned_schema(tmp_path):
    path = tmp_path / "lots.json"
    ledger = LotLedger(str(path))
    _lot(ledger, "lot-1", "sell_entropy", 0.1, 100.0, 101.0)
    ledger.save()
    payload = json.loads(path.read_text())
    assert payload["schema_version"] == 1
    assert payload["lots"][0]["lot_id"] == "lot-1"
