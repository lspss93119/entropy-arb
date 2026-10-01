"""Engine signal math: midline band directions, inventory ladder, scan.

Run:  python3 -m pytest tests/  (or  python3 tests/test_engine.py)
"""
import asyncio
import os
import sys
import tempfile
from dataclasses import replace
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import ArbPlan, OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg(midline=5.0, upper=4.0, lower=3.0, mode="fixed"):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    rolling = "" if mode == "fixed" else """
strategy:
  mode: rolling
rolling:
  window_hours: 12
  update_minutes: 15
  min_coverage_pct: 80
  seed_from_csv: false
  min_exit_capture_bps: 0
"""
    f.write(f"""
thresholds:
  midline_bps: {midline}
  upper_bps: {upper}
  lower_bps: {lower}
execution:
  premium_persist_sec: 0.0
{rolling}
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", hedge_venue="lighter-rh")


class StubVenue:
    def __init__(self, key, label, cap=10000.0, fee=0.0):
        self.key, self.name = key, label
        self.cap_usd, self.fee_bps = cap, fee
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.position, self.cash = 0.0, 0.0
        self.orders_per_min = 30
        self.last_traded_ts = 0.0
        self.book = OrderBook()

    def ready_to_trade(self):
        return True

    def set_book(self, bid, ask, sz=50.0):
        self.book.apply_hl([[{"px": str(bid), "sz": str(sz)}],
                            [{"px": str(ask), "sz": str(sz)}]])


def make_engine(mode="fixed", **thr):
    cfg = make_cfg(mode=mode, **thr)
    if mode == "rolling":
        runtime_dir = tempfile.mkdtemp(prefix="entropy-arb-runtime-")
        cfg.trades_csv = os.path.join(runtime_dir, "logs", "trades",
                                      "trades.csv")
    eng = Engine(cfg)
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    return eng


def approx(a, b, tol=1e-9):
    assert abs(a - b) <= tol, f"{a} != {b}"


def test_eff_threshold_directions():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    e, h = eng.entropy, eng.hedge
    # sell entropy: hurdle = midline + upper = 9
    approx(eng._eff_threshold(buy=h, sell=e), 9.0)
    # buy entropy: hurdle = lower - midline = -2 (unwind side of a positive
    # midline is deliberately cheap — that's what completes the round trip)
    approx(eng._eff_threshold(buy=e, sell=h), -2.0)
    # round trip nets upper + lower regardless of midline sign
    for m in (-7.0, 0.0, 12.5):
        eng.cfg.midline_bps = m
        total = eng._eff_threshold(buy=h, sell=e) + eng._eff_threshold(buy=e, sell=h)
        approx(total, 7.0)


def test_inventory_ladder():
    eng = make_engine()
    eng.cfg.inventory_scale_bps, eng.cfg.inventory_floor_frac = 10.0, 0.5
    e, h = eng.entropy, eng.hedge
    e.set_book(99.9, 100.1)   # mid 100
    h.set_book(99.9, 100.1)
    approx(eng._inv_add_bps(e, h), 0.0)          # flat: dead zone
    e.position = 90.0                             # long $9k of $10k cap
    v = eng._inv_add_bps(e, h)                    # buying entropy adds long
    assert 7.5 < v < 8.5, v                       # u=0.9 -> ~+8
    approx(eng._inv_add_bps(h, e), 0.0)           # selling entropy reduces
    h.position = -90.0                            # hedge short $9k too
    v2 = eng._inv_add_bps(e, h)                   # both legs add -> max()
    assert abs(v2 - v) < 0.6, (v, v2)             # max, not sum


def run_scan(eng):
    async def go():
        # first pass arms the direction, second passes the persistence gate
        # (premium_persist_sec is 0 in the test config)
        eng._scan(__import__("time").time())
        return eng._scan(__import__("time").time())
    return asyncio.run(go())


def seed_rolling(eng, now):
    block = int(now // 900) * 900
    for i in range(720):
        eng._rolling.ingest_row({
            "minute_ts": block - (720 - i) * 60,
            "samples": 1,
            "premium_close_bps": -1.0 if i % 2 == 0 else 1.0,
        })
    return block


def rolling_execution(direction, matched_qty, settled_ts,
                      reduce_only=False, lot_id="lot-1", buy_px=100.0,
                      sell_px=101.0):
    plan = ArbPlan(
        qty=max(matched_qty, 0.1), buy_limit=buy_px, sell_limit=sell_px,
        buy_notional=max(matched_qty, 0.1) * buy_px,
        sell_notional=max(matched_qty, 0.1) * sell_px,
        q_max=max(matched_qty, 0.1),
        q_max_notional=max(matched_qty, 0.1) * buy_px,
        top_premium_bps=0.0, marginal_premium_bps=0.0,
        buy_fee=0.0, sell_fee=0.0, reduce_only=reduce_only,
        lot_allocations=(({"lot_id": lot_id, "qty": matched_qty},)
                         if reduce_only else ()),
    )
    buy_name = "ENTROPY" if direction == "buy_entropy" else "RH"
    sell_name = "RH" if direction == "buy_entropy" else "ENTROPY"
    return {
        "ok": True,
        "event_id": f"event-{settled_ts:g}",
        "matched_qty": matched_qty,
        "plan": plan,
        "direction": direction,
        "settled_ts": settled_ts,
        "buy": SimpleNamespace(name=buy_name, fee_bps=0.0),
        "sell": SimpleNamespace(name=sell_name, fee_bps=0.0),
        "buy_info": {"avg_px": buy_px},
        "sell_info": {"avg_px": sell_px},
    }


def set_authoritative_positions(eng, entropy_qty, hedge_qty):
    eng.entropy.set_book(100.0, 100.1)
    eng.hedge.set_book(100.0, 100.1)

    async def fetch_entropy():
        return entropy_qty

    async def fetch_hedge():
        return hedge_qty

    eng.entropy.fetch_position = fetch_entropy
    eng.hedge.fetch_position = fetch_hedge


def seed_open_rolling_position(eng, direction="buy_entropy", qty=0.49):
    eng._update_rolling_position(
        rolling_execution(direction, qty, 100.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    if direction == "buy_entropy":
        eng.entropy.position = qty
        eng.hedge.position = -qty
    else:
        eng.entropy.position = -qty
        eng.hedge.position = qty
    eng._rolling_ledger_loaded = True


def partial_sell_entropy_reduce(first_leg="entropy"):
    execution = rolling_execution(
        "sell_entropy", 0.0, 200.0, reduce_only=True)
    execution["plan"] = replace(
        execution["plan"],
        qty=0.028,
        buy_notional=2.8,
        sell_notional=2.8,
        q_max=0.028,
        q_max_notional=2.8,
        lot_allocations=({"lot_id": "event-100", "qty": 0.028},),
    )
    execution["matched_qty"] = 0.0
    if first_leg == "entropy":
        execution["buy_info"] = {"avg_px": 101.0, "filled_base": 0.0}
        execution["sell_info"] = {"avg_px": 101.5, "filled_base": 0.028}
    else:
        execution["buy_info"] = {"avg_px": 101.0, "filled_base": 0.028}
        execution["sell_info"] = {"avg_px": 101.5, "filled_base": 0.0}
    return execution


def test_reconcile_allows_exact_active_partial_reduce_without_halting():
    eng = make_engine(mode="rolling")
    seed_open_rolling_position(eng)
    execution = partial_sell_entropy_reduce(first_leg="entropy")

    eng._begin_inflight_reconciliation(execution)
    set_authoritative_positions(eng, entropy_qty=0.462, hedge_qty=-0.49)

    asyncio.run(eng._reconcile_positions(hedge=False))

    assert eng.halted is False
    assert eng._rolling_ledger.total_qty == pytest.approx(0.49)


def test_execute_locked_keeps_reconcile_provisional_until_ledger_update():
    eng = make_engine(mode="rolling")
    seed_open_rolling_position(eng)
    execution = partial_sell_entropy_reduce(first_leg="entropy")
    execution["unresolved"] = False
    eng._log_csv = lambda *_args: None
    reconcile_seen = {}

    async def execute(_buy, _sell, _plan):
        eng.entropy.position = 0.462
        eng.hedge.position = -0.49
        set_authoritative_positions(eng, entropy_qty=0.462,
                                    hedge_qty=-0.49)
        return execution

    async def hedge():
        await eng._reconcile_positions(hedge=False)
        reconcile_seen["ledger_qty"] = eng._rolling_ledger.total_qty
        eng.hedge.position = -0.462
        return {
            "status": "filled", "filled_qty": 0.028, "venue": "RH",
            "side": "buy", "avg_px": 101.2, "remaining_net_qty": 0.0,
        }

    eng._execute = execute
    eng._maybe_hedge = hedge

    async def run_execution():
        await eng._vlock("entropy").acquire()
        await eng._vlock("hedge").acquire()
        await eng._execute_locked(eng.hedge, eng.entropy,
                                  execution["plan"])

    asyncio.run(run_execution())

    assert reconcile_seen["ledger_qty"] == pytest.approx(0.49)
    assert eng._rolling_ledger.total_qty == pytest.approx(0.462)
    assert eng._inflight_reconciliation is None
    assert eng.halted is False


def test_reconcile_passes_after_active_reduce_closes_ledger():
    eng = make_engine(mode="rolling")
    seed_open_rolling_position(eng)
    execution = partial_sell_entropy_reduce(first_leg="entropy")
    eng._begin_inflight_reconciliation(execution)
    hedge = {
        "status": "filled", "filled_qty": 0.028, "venue": "RH",
        "side": "buy", "avg_px": 101.2, "remaining_net_qty": 0.0,
    }
    eng._advance_inflight_reconciliation(hedge)
    eng.entropy.position = 0.462
    eng.hedge.position = -0.462
    eng._update_rolling_position(execution, hedge)
    eng._finish_inflight_reconciliation()
    set_authoritative_positions(eng, entropy_qty=0.462, hedge_qty=-0.462)

    asyncio.run(eng._reconcile_positions(hedge=False))

    assert eng.halted is False
    assert eng._rolling_ledger.total_qty == pytest.approx(0.462)


def test_reconcile_still_halts_unexplained_mismatch():
    eng = make_engine(mode="rolling")
    seed_open_rolling_position(eng)
    set_authoritative_positions(eng, entropy_qty=0.462, hedge_qty=-0.49)

    asyncio.run(eng._reconcile_positions(hedge=False))

    assert eng.halted is True


def test_reconcile_halts_when_active_mismatch_quantity_is_wrong():
    eng = make_engine(mode="rolling")
    seed_open_rolling_position(eng)
    execution = partial_sell_entropy_reduce(first_leg="entropy")
    eng._begin_inflight_reconciliation(execution)
    set_authoritative_positions(eng, entropy_qty=0.44, hedge_qty=-0.49)

    asyncio.run(eng._reconcile_positions(hedge=False))

    assert eng.halted is True


@pytest.mark.parametrize("status", ["send-failed", "timeout", "unresolved"])
def test_reconcile_halts_when_active_hedge_fails(status):
    eng = make_engine(mode="rolling")
    seed_open_rolling_position(eng)
    execution = partial_sell_entropy_reduce(first_leg="entropy")
    eng._begin_inflight_reconciliation(execution)
    eng._advance_inflight_reconciliation({
        "status": status, "filled_qty": 0.0,
        "remaining_net_qty": 0.028,
    })
    set_authoritative_positions(eng, entropy_qty=0.462, hedge_qty=-0.49)

    asyncio.run(eng._reconcile_positions(hedge=False))

    assert eng.halted is True


def test_reconcile_halts_when_mismatch_remains_after_execution_finishes():
    eng = make_engine(mode="rolling")
    seed_open_rolling_position(eng)
    execution = partial_sell_entropy_reduce(first_leg="entropy")
    eng._begin_inflight_reconciliation(execution)
    hedge = {
        "status": "filled", "filled_qty": 0.028, "venue": "RH",
        "side": "buy", "avg_px": 101.2, "remaining_net_qty": 0.0,
    }
    eng._advance_inflight_reconciliation(hedge)
    eng.entropy.position = 0.45
    eng.hedge.position = -0.462
    eng._update_rolling_position(execution, hedge)
    eng._finish_inflight_reconciliation()
    set_authoritative_positions(eng, entropy_qty=0.45, hedge_qty=-0.462)

    asyncio.run(eng._reconcile_positions(hedge=False))

    assert eng.halted is True


def test_reconcile_allows_exact_active_partial_reduce_when_rh_fills_first():
    eng = make_engine(mode="rolling")
    seed_open_rolling_position(eng)
    execution = partial_sell_entropy_reduce(first_leg="rh")
    eng._begin_inflight_reconciliation(execution)
    set_authoritative_positions(eng, entropy_qty=0.49, hedge_qty=-0.462)

    asyncio.run(eng._reconcile_positions(hedge=False))

    assert eng.halted is False
    assert eng._rolling_ledger.total_qty == pytest.approx(0.49)


def test_reconcile_allows_inflight_entry_without_mutating_empty_ledger():
    eng = make_engine(mode="rolling")
    eng._rolling_ledger_loaded = True
    execution = rolling_execution("buy_entropy", 0.0, 300.0)
    execution["buy_info"] = {"avg_px": 101.0, "filled_base": 0.028}
    execution["sell_info"] = {"avg_px": 101.5, "filled_base": 0.0}
    execution["matched_qty"] = 0.0
    eng._begin_inflight_reconciliation(execution)
    set_authoritative_positions(eng, entropy_qty=0.028, hedge_qty=0.0)

    asyncio.run(eng._reconcile_positions(hedge=False))

    assert eng.halted is False
    assert eng._rolling_ledger.total_qty == pytest.approx(0.0)


def test_provisional_reconciliation_does_not_clear_existing_halt():
    eng = make_engine(mode="rolling")
    seed_open_rolling_position(eng)
    execution = partial_sell_entropy_reduce(first_leg="entropy")
    eng._begin_inflight_reconciliation(execution)
    eng.halted = True
    set_authoritative_positions(eng, entropy_qty=0.462, hedge_qty=-0.49)

    asyncio.run(eng._reconcile_positions(hedge=False))

    assert eng.halted is True


def test_scan_fires_sell_entropy_above_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 15 bps rich vs hedge: above midline+upper=9 -> sell entropy
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert sell.key == "entropy" and buy.key == "hedge"
    assert plan.exp_edge_usd > 0


def test_scan_quiet_inside_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 5 bps rich = exactly on the midline: inside the band, no trade
    eng.entropy.set_book(100.04, 100.06)
    eng.hedge.set_book(99.99, 100.01)
    assert run_scan(eng) is None


def test_scan_fires_buy_entropy_below_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 5 bps CHEAP (premium -5): below midline-lower=+2 -> buy entropy
    eng.entropy.set_book(99.94, 99.96)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert buy.key == "entropy" and sell.key == "hedge"


def test_scan_respects_position_caps():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng.entropy.position = -100.0   # entropy already short at its cap
    eng.entropy.cap_usd = 10000.0
    eng.hedge.position = 100.0
    eng.hedge.cap_usd = 10000.0
    assert run_scan(eng) is None


def test_rolling_scan_enters_from_valid_snapshot():
    eng = make_engine(mode="rolling")
    seed_rolling(eng, __import__("time").time())
    eng.entropy.set_book(100.11, 100.13)
    eng.hedge.set_book(99.99, 100.01)

    best = run_scan(eng)

    assert best is not None
    buy, sell, plan = best
    assert sell.key == "entropy" and buy.key == "hedge"
    assert plan.reduce_only is False
    assert eng._rolling_signal_meta["reason"] == "entry"
    assert eng._rolling_signal_meta["direction"] == "sell_entropy"


@pytest.mark.parametrize(
    ("direction", "entropy_book", "hedge_book"),
    [
        ("sell_entropy", (100.11, 100.13), (99.99, 100.01)),
        ("buy_entropy", (99.87, 99.89), (99.99, 100.01)),
    ],
)
def test_rolling_scan_allows_same_direction_entry_for_open_inventory(
        direction, entropy_book, hedge_book):
    eng = make_engine(mode="rolling")
    now = __import__("time").time()
    seed_rolling(eng, now)
    eng._rolling_open_direction = direction
    eng._rolling_open_qty = 1.0
    eng._rolling_entry_ts = now - 60.0
    eng.entropy.set_book(*entropy_book)
    eng.hedge.set_book(*hedge_book)

    best = run_scan(eng)

    assert best is not None
    buy, sell, plan = best
    assert plan.reduce_only is False
    assert eng._rolling_signal_meta["direction"] == direction
    if direction == "sell_entropy":
        assert buy.key == "hedge" and sell.key == "entropy"
    else:
        assert buy.key == "entropy" and sell.key == "hedge"


def test_rolling_opposite_signal_plans_reduce_only_close():
    eng = make_engine(mode="rolling")
    now = __import__("time").time()
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 1.0, now - 60.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    seed_rolling(eng, now)
    # Premium is below the dynamic center band, so it is the opposite
    # direction and must close the existing sell_entropy inventory.
    eng.entropy.set_book(99.89, 99.91)
    eng.hedge.set_book(99.99, 100.01)

    best = run_scan(eng)

    assert best is not None
    buy, sell, plan = best
    assert buy.key == "entropy" and sell.key == "hedge"
    assert plan.reduce_only is True
    assert eng._rolling_signal_meta["action"] == "reduce"
    assert eng._rolling_open_direction == "sell_entropy"
    assert eng._rolling_open_qty == 1.0


@pytest.mark.parametrize(
    ("direction", "blocked_entropy", "blocked_hedge", "allowed_entropy",
     "allowed_hedge", "entry_buy_px", "entry_sell_px"),
    [
        (
            "sell_entropy",
            (99.0, 99.99), (99.99, 100.01),
            (99.0, 99.0), (99.99, 100.01),
            100.0, 100.0,
        ),
        (
            "buy_entropy",
            (100.0, 101.0), (99.99, 100.01),
            (101.0, 101.0), (99.99, 100.01),
            99.0, 100.0,
        ),
    ],
)
def test_rolling_reduce_requires_executable_opposite_dynamic_threshold(
        direction, blocked_entropy, blocked_hedge, allowed_entropy,
        allowed_hedge, entry_buy_px, entry_sell_px):
    eng = make_engine(mode="rolling")
    now = __import__("time").time()
    eng._update_rolling_position(
        rolling_execution(direction, 1.0, now - 60.0,
                          buy_px=entry_buy_px, sell_px=entry_sell_px),
        {"status": "filled", "filled_qty": 0.0},
    )
    seed_rolling(eng, now)

    eng.entropy.set_book(*blocked_entropy)
    eng.hedge.set_book(*blocked_hedge)
    assert run_scan(eng) is None

    eng.entropy.set_book(*allowed_entropy)
    eng.hedge.set_book(*allowed_hedge)
    best = run_scan(eng)

    assert best is not None
    buy, sell, plan = best
    assert plan.reduce_only is True
    if direction == "sell_entropy":
        assert buy.key == "entropy" and sell.key == "hedge"
    else:
        assert buy.key == "hedge" and sell.key == "entropy"


def test_rolling_reduce_threshold_excludes_inventory_surcharge(monkeypatch):
    eng = make_engine(mode="rolling")
    now = __import__("time").time()
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 1.0, now - 60.0,
                          buy_px=100.0, sell_px=100.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    seed_rolling(eng, now)
    monkeypatch.setattr(eng, "_inv_add_bps", lambda *_: 1_000.0)
    eng.entropy.set_book(99.7, 99.7)
    eng.hedge.set_book(99.99, 100.01)

    best = run_scan(eng)

    assert best is not None
    assert best[2].reduce_only is True


def test_rolling_reduce_gate_includes_both_venue_fees():
    eng = make_engine(mode="rolling")
    now = __import__("time").time()
    eng._update_rolling_position(
        rolling_execution("buy_entropy", 1.0, now - 60.0,
                          buy_px=99.0, sell_px=100.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    seed_rolling(eng, now)
    eng.entropy.fee_bps = 2.0
    eng.hedge.fee_bps = 3.0

    # Gross BBO premium is 8 bps, below the 4 bps dynamic hurdle plus 5 bps
    # of fees, even though the mid/mid signal is already on the exit side.
    eng.entropy.set_book(100.08, 100.08)
    eng.hedge.set_book(100.0, 100.0)
    assert run_scan(eng) is None

    # At 10 bps gross, the executable opposite threshold is satisfied.
    eng.entropy.set_book(100.10, 100.10)
    best = run_scan(eng)
    assert best is not None
    assert best[2].reduce_only is True


def test_rolling_add_entry_uses_inventory_surcharge(monkeypatch):
    eng = make_engine(mode="rolling")
    eng.cfg.inventory_scale_bps = 10.0
    eng.cfg.inventory_floor_frac = 0.5
    now = __import__("time").time()
    seed_rolling(eng, now)
    eng._rolling_open_direction = "sell_entropy"
    eng._rolling_open_qty = 1.0
    eng._rolling_entry_ts = now - 60.0
    eng.hedge.position = 90.0
    eng.entropy.position = -90.0
    eng.entropy.set_book(100.19, 100.21)
    eng.hedge.set_book(99.99, 100.01)
    thresholds = []
    original_plan = eng._plan

    def spy_plan(buy, sell, cap_notional, **kwargs):
        thresholds.append(kwargs.get("threshold_bps"))
        return original_plan(buy, sell, cap_notional, **kwargs)

    monkeypatch.setattr(eng, "_plan", spy_plan)

    best = run_scan(eng)

    assert best is not None
    # center=0, sell upper=4, plus the 8 bps inventory surcharge.
    assert thresholds[-1] == pytest.approx(12.0, abs=0.2)


def test_rolling_add_entry_respects_position_headroom():
    eng = make_engine(mode="rolling")
    eng.cfg.inventory_scale_bps = 0.0
    now = __import__("time").time()
    seed_rolling(eng, now)
    eng._rolling_open_direction = "sell_entropy"
    eng._rolling_open_qty = 1.0
    eng._rolling_entry_ts = now - 60.0
    eng.hedge.position = 99.0
    eng.entropy.position = -99.0
    eng.entropy.set_book(100.11, 100.13)
    eng.hedge.set_book(99.99, 100.01)

    best = run_scan(eng)

    assert best is not None
    buy, sell, plan = best
    headroom = eng._headroom(buy, sell, plan.buy_limit)
    assert plan.buy_notional <= headroom + 1e-6


def test_rolling_same_direction_fill_accumulates_and_preserves_cycle_timeout(
        caplog):
    eng = make_engine(mode="rolling")
    first_entry_ts = 100.0
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 0.4, first_entry_ts),
        {"status": "filled", "filled_qty": 0.4},
    )
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 0.6, 200.0, lot_id="lot-2"),
        {"status": "filled", "filled_qty": 0.6},
    )

    assert eng._rolling_open_direction == "sell_entropy"
    assert eng._rolling_open_qty == pytest.approx(1.0)
    assert eng._rolling_entry_ts == first_entry_ts
    assert len(eng._rolling_ledger.lots) == 2
    assert "second entry" not in caplog.text


def test_rolling_reduce_only_exit_closes_accumulated_inventory():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("buy_entropy", 0.75, 100.0),
        {"status": "filled", "filled_qty": 0.75},
    )
    eng._update_rolling_position(
        rolling_execution("buy_entropy", 0.75, 200.0, lot_id="lot-2"),
        {"status": "filled", "filled_qty": 0.75},
    )
    exit_execution = rolling_execution(
        "sell_entropy", 0.5, 300.0, reduce_only=True, lot_id="event-100")

    eng._update_rolling_position(
        exit_execution, {"status": "filled", "filled_qty": 0.0})

    assert eng._rolling_open_direction == "buy_entropy"
    assert eng._rolling_open_qty == pytest.approx(1.0)
    assert eng._rolling_entry_ts == 100.0

    final = rolling_execution("sell_entropy", 1.0, 400.0,
                              reduce_only=True, lot_id="lot-1")
    final["plan"] = replace(
        final["plan"],
        lot_allocations=({"lot_id": "event-100", "qty": 0.25},
                         {"lot_id": "event-200", "qty": 0.75}),
    )
    eng._update_rolling_position(final, {"status": "filled", "filled_qty": 0.0})

    assert eng._rolling_open_direction is None
    assert eng._rolling_open_qty == 0.0
    assert eng._rolling_entry_ts is None
    assert not eng._rolling_ledger.lots


def test_rolling_state_rejects_opposite_entry_without_changing_inventory():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 1.0, 100.0),
        {"status": "filled", "filled_qty": 1.0},
    )

    eng._update_rolling_position(
        rolling_execution("buy_entropy", 0.5, 200.0),
        {"status": "filled", "filled_qty": 0.5},
    )

    assert eng._rolling_open_direction == "sell_entropy"
    assert eng._rolling_open_qty == pytest.approx(1.0)
    assert eng._rolling_entry_ts == 100.0
    assert eng.halted is True


def test_rolling_scan_exits_on_reversal_to_mean():
    eng = make_engine(mode="rolling")
    now = __import__("time").time()
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 1.0, now - 60.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    seed_rolling(eng, now)
    eng.entropy.set_book(99.89, 99.91)
    eng.hedge.set_book(99.99, 100.01)

    best = run_scan(eng)

    assert best is not None
    buy, sell, plan = best
    assert buy.key == "entropy" and sell.key == "hedge"
    assert plan.reduce_only is True
    assert eng._rolling_signal_meta["reason"] == "entry"
    assert eng._rolling_signal_meta["action"] == "reduce"


def test_rolling_has_no_timeout_exit_without_valid_snapshot():
    eng = make_engine(mode="rolling")
    now = __import__("time").time()
    eng._rolling_open_direction = "sell_entropy"
    eng._rolling_open_qty = 1.0
    eng._rolling_entry_ts = now - 12 * 3600 - 1.0
    eng.entropy.set_book(100.11, 100.13)
    eng.hedge.set_book(99.99, 100.01)

    best = run_scan(eng)

    assert best is None
    assert eng.halted is False


def test_rolling_invalid_snapshot_blocks_new_entry():
    eng = make_engine(mode="rolling")
    eng.entropy.set_book(100.11, 100.13)
    eng.hedge.set_book(99.99, 100.01)

    assert run_scan(eng) is None


def test_rolling_start_rejects_nonflat_positions():
    eng = make_engine(mode="rolling")
    eng.entropy.position = 0.01

    with pytest.raises(RuntimeError, match="ledger|flat"):
        eng._check_rolling_start_state()


def test_rolling_start_loads_matching_persisted_lots():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 0.5, 100.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    eng.entropy.position = -0.5
    eng.hedge.position = 0.5
    eng._rolling_ledger_loaded = False

    eng._check_rolling_start_state()

    assert eng._rolling_ledger_loaded is True
    assert eng._rolling_open_direction == "sell_entropy"
    assert eng._rolling_open_qty == pytest.approx(0.5)


def test_rolling_start_rejects_stale_ledger_when_positions_are_flat():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 0.5, 100.0),
        {"status": "filled", "filled_qty": 0.0},
    )

    with pytest.raises(RuntimeError, match="position mismatch"):
        eng._check_rolling_start_state()


def test_rolling_start_rejects_corrupt_ledger():
    eng = make_engine(mode="rolling")
    os.makedirs(os.path.dirname(eng._rolling_ledger.path), exist_ok=True)
    with open(eng._rolling_ledger.path, "w") as fh:
        fh.write("not-json")

    with pytest.raises(RuntimeError, match="ledger validation failed"):
        eng._check_rolling_start_state()


def test_rolling_entry_persistence_failure_halts_without_mutating_state(
        monkeypatch):
    eng = make_engine(mode="rolling")

    def fail_persist(_lots):
        from entropy_arb.lot_ledger import LotLedgerError
        raise LotLedgerError("disk full")

    monkeypatch.setattr(eng._rolling_ledger, "_persist", fail_persist)
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 0.5, 100.0),
        {"status": "filled", "filled_qty": 0.0},
    )

    assert eng.halted is True
    assert not eng._rolling_ledger.lots
    assert eng._rolling_open_qty == 0.0


def test_rolling_live_requires_recorder():
    eng = make_engine(mode="rolling")
    eng.cfg.recorder_enabled = False

    with pytest.raises(RuntimeError, match="recorder.enabled=true"):
        eng._validate_rolling_runtime(live=True)


def test_rolling_blocks_entry_on_residual_position():
    eng = make_engine(mode="rolling")
    seed_rolling(eng, __import__("time").time())
    eng.entropy.position = 0.01
    eng.entropy.set_book(100.11, 100.13)
    eng.hedge.set_book(99.99, 100.01)

    assert run_scan(eng) is None


def test_rolling_failure_halts_engine():
    eng = make_engine(mode="rolling")

    eng._halt_rolling("test unresolved fill")

    assert eng.halted is True


def test_rolling_unresolved_execution_halts_before_next_scan():
    eng = make_engine(mode="rolling")
    eng._log_csv = lambda execution, hedge: None

    async def unresolved_execute(buy, sell, plan):
        return {"unresolved": True, "ok": False}

    eng._execute = unresolved_execute

    async def run_execution():
        await eng._vlock("entropy").acquire()
        await eng._vlock("hedge").acquire()
        await eng._execute_locked(eng.entropy, eng.hedge, None)

    asyncio.run(run_execution())

    assert eng.halted is True


def test_rolling_partial_exit_keeps_state_until_residual_is_hedged():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 1.0, 100.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    execution = rolling_execution("buy_entropy", 0.4, 200.0,
                                  reduce_only=True, lot_id="event-100")

    eng._update_rolling_position(execution, {
        "status": "unhedgeable", "filled_qty": 0.0,
    })
    assert eng._rolling_open_qty == 1.0
    assert eng.halted is True

    eng.halted = False
    repair = rolling_execution("buy_entropy", 0.0, 300.0,
                               reduce_only=True, lot_id="event-100")
    repair["plan"] = replace(
        repair["plan"], qty=0.4, buy_notional=40.0, sell_notional=40.0,
        lot_allocations=({"lot_id": "event-100", "qty": 0.4},))
    eng._update_rolling_position(repair, {
        "status": "filled", "filled_qty": 0.4,
    })
    assert eng._rolling_open_direction == "sell_entropy"
    assert eng._rolling_open_qty == pytest.approx(0.6)


def test_rolling_partial_exit_counts_successful_residual_hedge():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 1.0, 100.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    execution = rolling_execution("buy_entropy", 0.3, 200.0,
                                  reduce_only=True, lot_id="event-100")
    execution["plan"] = replace(
        execution["plan"], qty=0.4, buy_notional=40.0, sell_notional=40.4,
        lot_allocations=({"lot_id": "event-100", "qty": 0.4},))

    eng._update_rolling_position(execution, {
        "status": "filled", "filled_qty": 0.1,
        "venue": "RH", "avg_px": 102.0,
    })

    assert eng._rolling_open_qty == pytest.approx(0.6)
    assert eng._rolling_result_meta["closed_qty"] == pytest.approx(0.4)


def test_rolling_complete_actual_exit_fills_populate_realized_capture():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 1.0, 100.0,
                          buy_px=100.0, sell_px=102.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    exit_execution = rolling_execution(
        "buy_entropy", 1.0, 200.0, reduce_only=True,
        lot_id="event-100", buy_px=101.0, sell_px=101.5)

    eng._update_rolling_position(
        exit_execution, {"status": "filled", "filled_qty": 0.0})

    assert eng._rolling_result_meta["realized_capture_usd"] == pytest.approx(2.5)
    assert eng._rolling_result_meta["realized_capture_bps"] == pytest.approx(
        2.5 / 101.0 * 1e4)
    assert eng._rolling_open_qty == 0.0


def test_rolling_residual_hedge_actual_price_is_blended_into_capture():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 1.0, 100.0,
                          buy_px=100.0, sell_px=102.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    exit_execution = rolling_execution(
        "buy_entropy", 0.75, 200.0, reduce_only=True,
        buy_px=101.0, sell_px=101.5)
    exit_execution["plan"] = replace(
        exit_execution["plan"], qty=1.0,
        lot_allocations=({"lot_id": "event-100", "qty": 1.0},))
    _set_reduce_fills(exit_execution, buy_fill=1.0, buy_px=101.0,
                      sell_fill=0.75, sell_px=101.5)

    eng._update_rolling_position(
        exit_execution,
        {"status": "filled", "filled_qty": 0.25,
         "venue": "RH", "avg_px": 102.5},
    )

    # Entry cash is +2.0/base. Exit sell price is the blend of .75 at 101.5
    # and .25 at 102.5, while the actual buy price is 101.0.
    assert eng._rolling_result_meta["realized_capture_usd"] == pytest.approx(2.75)
    assert eng._rolling_open_qty == 0.0


def _set_reduce_fills(execution, *, buy_fill, buy_px, sell_fill, sell_px):
    execution["matched_qty"] = min(buy_fill, sell_fill)
    execution["buy_info"] = {
        "avg_px": buy_px if buy_fill else None,
        "filled_base": buy_fill,
    }
    execution["sell_info"] = {
        "avg_px": sell_px if sell_fill else None,
        "filled_base": sell_fill,
    }


def test_rolling_reduce_capture_uses_full_residual_hedge_buy_entropy():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 0.028, 100.0,
                          buy_px=100.0, sell_px=102.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    execution = rolling_execution(
        "buy_entropy", 0.0, 200.0, reduce_only=True,
        lot_id="event-100", buy_px=99.0, sell_px=101.5)
    execution["plan"] = replace(
        execution["plan"], qty=0.028,
        lot_allocations=({"lot_id": "event-100", "qty": 0.028},))
    _set_reduce_fills(execution, buy_fill=0.0, buy_px=None,
                      sell_fill=0.028, sell_px=101.5)

    eng._update_rolling_position(execution, {
        "status": "filled", "filled_qty": 0.028,
        "venue": "ENTROPY", "side": "buy", "avg_px": 99.0,
        "remaining_net_qty": 0.0,
    })

    assert eng._rolling_result_meta["closed_qty"] == pytest.approx(0.028)
    assert eng._rolling_result_meta["realized_capture_usd"] == pytest.approx(
        0.028 * 4.5)
    assert eng._rolling_result_meta["realized_capture_bps"] > 0


def test_rolling_reduce_capture_uses_full_residual_hedge_sell_entropy():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("buy_entropy", 0.028, 100.0,
                          buy_px=99.0, sell_px=101.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    execution = rolling_execution(
        "sell_entropy", 0.0, 200.0, reduce_only=True,
        lot_id="event-100", buy_px=99.0, sell_px=101.5)
    execution["plan"] = replace(
        execution["plan"], qty=0.028,
        lot_allocations=({"lot_id": "event-100", "qty": 0.028},))
    _set_reduce_fills(execution, buy_fill=0.028, buy_px=99.0,
                      sell_fill=0.0, sell_px=None)

    eng._update_rolling_position(execution, {
        "status": "filled", "filled_qty": 0.028,
        "venue": "ENTROPY", "side": "sell", "avg_px": 101.5,
        "remaining_net_qty": 0.0,
    })

    assert eng._rolling_result_meta["closed_qty"] == pytest.approx(0.028)
    assert eng._rolling_result_meta["realized_capture_usd"] == pytest.approx(
        0.028 * 4.5)
    assert eng._rolling_result_meta["realized_capture_bps"] > 0


def test_rolling_reduce_capture_blends_partial_primary_and_hedge_remainder():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 0.024, 100.0,
                          buy_px=100.0, sell_px=102.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    execution = rolling_execution(
        "buy_entropy", 0.010, 200.0, reduce_only=True,
        lot_id="event-100", buy_px=99.0, sell_px=101.0)
    execution["plan"] = replace(
        execution["plan"], qty=0.024,
        lot_allocations=({"lot_id": "event-100", "qty": 0.024},))
    _set_reduce_fills(execution, buy_fill=0.024, buy_px=99.0,
                      sell_fill=0.010, sell_px=101.0)

    hedge_fills = ((0.006, 101.5), (0.008, 102.375))
    hedge_qty = sum(qty for qty, _ in hedge_fills)
    hedge_px = sum(qty * px for qty, px in hedge_fills) / hedge_qty
    eng._update_rolling_position(execution, {
        "status": "filled", "filled_qty": hedge_qty,
        "venue": "RH", "side": "sell", "avg_px": hedge_px,
        "remaining_net_qty": 0.0,
    })

    effective_sell = (0.010 * 101.0 + hedge_qty * hedge_px) / 0.024
    expected = 0.024 * (2.0 + effective_sell - 99.0)
    assert eng._rolling_result_meta["closed_qty"] == pytest.approx(0.024)
    assert eng._rolling_result_meta["realized_capture_usd"] == pytest.approx(
        expected)
    assert eng._rolling_open_qty == 0.0


def test_rolling_reduce_capture_uses_actual_partial_hedge_closed_qty():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 0.028, 100.0,
                          buy_px=100.0, sell_px=102.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    execution = rolling_execution(
        "buy_entropy", 0.0, 200.0, reduce_only=True,
        lot_id="event-100", buy_px=99.0, sell_px=101.0)
    execution["plan"] = replace(
        execution["plan"], qty=0.028,
        lot_allocations=({"lot_id": "event-100", "qty": 0.028},))
    _set_reduce_fills(execution, buy_fill=0.020, buy_px=99.0,
                      sell_fill=0.0, sell_px=None)

    eng._update_rolling_position(execution, {
        "status": "filled", "filled_qty": 0.020,
        "venue": "RH", "side": "sell", "avg_px": 102.0,
        "remaining_net_qty": 0.0,
    })

    assert eng._rolling_result_meta["closed_qty"] == pytest.approx(0.020)
    assert eng._rolling_result_meta["realized_capture_usd"] == pytest.approx(
        0.020 * 5.0)
    assert eng._rolling_open_qty == pytest.approx(0.008)


def test_rolling_reduce_capture_is_unavailable_when_hedge_unsettled(caplog):
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 0.028, 100.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    execution = rolling_execution(
        "buy_entropy", 0.0, 200.0, reduce_only=True,
        lot_id="event-100")
    execution["plan"] = replace(
        execution["plan"], qty=0.028,
        lot_allocations=({"lot_id": "event-100", "qty": 0.028},))
    _set_reduce_fills(execution, buy_fill=0.0, buy_px=None,
                      sell_fill=0.028, sell_px=101.0)

    eng._update_rolling_position(execution, {
        "status": "unresolved", "filled_qty": 0.0,
        "remaining_net_qty": 0.028,
    })

    assert eng.halted is True
    assert eng._rolling_ledger.total_qty == pytest.approx(0.028)
    assert eng._rolling_result_meta.get("realized_capture_usd") is None
    assert eng._rolling_result_meta.get("realized_capture_bps") is None


def test_rolling_missing_actual_exit_price_closes_ledger_without_capture(
        caplog):
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 1.0, 100.0,
                          buy_px=100.0, sell_px=102.0),
        {"status": "filled", "filled_qty": 0.0},
    )
    exit_execution = rolling_execution(
        "buy_entropy", 1.0, 200.0, reduce_only=True,
        lot_id="event-100", buy_px=101.0, sell_px=101.5)
    exit_execution["buy_info"]["avg_px"] = None

    eng._update_rolling_position(
        exit_execution, {"status": "filled", "filled_qty": 0.0})

    assert eng._rolling_result_meta["realized_capture_usd"] is None
    assert eng._rolling_result_meta["realized_capture_bps"] is None
    assert eng._rolling_open_qty == 0.0
    assert "realized capture unavailable" in caplog.text


def test_rolling_filled_hedge_with_residual_net_position_halts():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("sell_entropy", 1.0, 100.0),
        {"status": "filled", "filled_qty": 1.0,
         "remaining_net_qty": 0.01},
    )

    assert eng.halted is True
    assert not eng._rolling_ledger.lots


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
