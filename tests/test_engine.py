"""Engine signal math: midline band directions, inventory ladder, scan.

Run:  python3 -m pytest tests/  (or  python3 tests/test_engine.py)
"""
import asyncio
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import ArbPlan, OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from entropy_arb.rolling import RollingSignal  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg(midline=5.0, upper=4.0, lower=3.0, mode="fixed"):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    rolling = "" if mode == "fixed" else """
strategy:
  mode: rolling
rolling:
  window_hours: 12
  update_minutes: 15
  entry_z: 1.5
  exit_z: 0.5
  min_reversion_bps: 5
  max_spread_bps: 10
  min_coverage_pct: 80
  timeout_hours: 12
  seed_from_csv: false
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
                      reduce_only=False):
    plan = ArbPlan(
        qty=max(matched_qty, 1.0), buy_limit=100.0, sell_limit=100.0,
        buy_notional=100.0, sell_notional=100.0,
        q_max=1.0, q_max_notional=100.0,
        top_premium_bps=0.0, marginal_premium_bps=0.0,
        buy_fee=0.0, sell_fee=0.0, reduce_only=reduce_only,
    )
    return {
        "ok": True,
        "matched_qty": matched_qty,
        "plan": plan,
        "direction": direction,
        "settled_ts": settled_ts,
    }


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


def test_rolling_open_inventory_rejects_opposite_entry_signal(monkeypatch):
    eng = make_engine(mode="rolling")
    now = __import__("time").time()
    eng._rolling_open_direction = "sell_entropy"
    eng._rolling_open_qty = 1.0
    eng._rolling_entry_ts = now - 60.0
    eng.entropy.set_book(100.11, 100.13)
    eng.hedge.set_book(99.99, 100.01)
    entry_calls = []

    def no_exit(*args):
        return None

    def opposite_entry(*args):
        entry_calls.append(args)
        return RollingSignal(
            "buy_entropy", "entry", -2.0, now, 0.0, 1.0, 100.0, 720)

    monkeypatch.setattr(eng._rolling, "exit_signal", no_exit)
    monkeypatch.setattr(eng._rolling, "entry_signal", opposite_entry)

    assert run_scan(eng) is None
    assert entry_calls
    assert eng._rolling_open_direction == "sell_entropy"
    assert eng._rolling_open_qty == 1.0


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
    eng.entropy.set_book(100.11, 100.13)
    eng.hedge.set_book(99.99, 100.01)
    thresholds = []
    original_plan = eng._plan

    def spy_plan(buy, sell, cap_notional, **kwargs):
        thresholds.append(kwargs.get("threshold_bps"))
        return original_plan(buy, sell, cap_notional, **kwargs)

    monkeypatch.setattr(eng, "_plan", spy_plan)

    best = run_scan(eng)

    assert best is not None
    assert thresholds[-1] == pytest.approx(8.0, abs=0.2)


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
        rolling_execution("sell_entropy", 0.6, 200.0),
        {"status": "filled", "filled_qty": 0.6},
    )

    assert eng._rolling_open_direction == "sell_entropy"
    assert eng._rolling_open_qty == pytest.approx(1.0)
    assert eng._rolling_entry_ts == first_entry_ts
    assert "second entry" not in caplog.text


def test_rolling_reduce_only_exit_closes_accumulated_inventory():
    eng = make_engine(mode="rolling")
    eng._update_rolling_position(
        rolling_execution("buy_entropy", 0.75, 100.0),
        {"status": "filled", "filled_qty": 0.75},
    )
    eng._update_rolling_position(
        rolling_execution("buy_entropy", 0.75, 200.0),
        {"status": "filled", "filled_qty": 0.75},
    )
    exit_execution = rolling_execution(
        "sell_entropy", 0.5, 300.0, reduce_only=True)

    eng._update_rolling_position(
        exit_execution, {"status": "filled", "filled_qty": 0.0})

    assert eng._rolling_open_direction == "buy_entropy"
    assert eng._rolling_open_qty == pytest.approx(1.0)
    assert eng._rolling_entry_ts == 100.0

    eng._update_rolling_position(
        rolling_execution("sell_entropy", 1.0, 400.0, reduce_only=True),
        {"status": "filled", "filled_qty": 0.0},
    )

    assert eng._rolling_open_direction is None
    assert eng._rolling_open_qty == 0.0
    assert eng._rolling_entry_ts is None


def test_rolling_state_rejects_opposite_entry_without_changing_inventory(
        caplog):
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
    assert "opposite entry" in caplog.text


def test_rolling_scan_exits_on_reversal_to_mean():
    eng = make_engine(mode="rolling")
    now = __import__("time").time()
    seed_rolling(eng, now)
    eng._rolling_open_direction = "sell_entropy"
    eng._rolling_open_qty = 1.0
    eng._rolling_entry_ts = now - 60.0
    eng.entropy.set_book(99.99, 100.01)
    eng.hedge.set_book(99.99, 100.01)

    best = run_scan(eng)

    assert best is not None
    buy, sell, plan = best
    assert buy.key == "entropy" and sell.key == "hedge"
    assert plan.reduce_only is True
    assert eng._rolling_signal_meta["reason"] == "exit_z"


def test_rolling_timeout_can_force_close_without_valid_snapshot():
    eng = make_engine(mode="rolling")
    now = __import__("time").time()
    eng._rolling_open_direction = "sell_entropy"
    eng._rolling_open_qty = 1.0
    eng._rolling_entry_ts = now - 12 * 3600 - 1.0
    eng.entropy.set_book(100.11, 100.13)
    eng.hedge.set_book(99.99, 100.01)

    best = run_scan(eng)

    assert best is not None
    assert best[2].reduce_only is True
    assert eng._rolling_signal_meta["reason"] == "timeout"


def test_rolling_invalid_snapshot_blocks_new_entry():
    eng = make_engine(mode="rolling")
    eng.entropy.set_book(100.11, 100.13)
    eng.hedge.set_book(99.99, 100.01)

    assert run_scan(eng) is None


def test_rolling_start_rejects_nonflat_positions():
    eng = make_engine(mode="rolling")
    eng.entropy.position = 0.01

    with pytest.raises(RuntimeError, match="flat"):
        eng._check_rolling_start_flat()


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
    eng._rolling_open_direction = "sell_entropy"
    eng._rolling_open_qty = 1.0
    eng._rolling_entry_ts = __import__("time").time() - 60.0
    from entropy_arb.book import ArbPlan
    exit_plan = ArbPlan(
        qty=1.0, buy_limit=100.0, sell_limit=100.0,
        buy_notional=100.0, sell_notional=100.0,
        q_max=1.0, q_max_notional=100.0,
        top_premium_bps=0.0, marginal_premium_bps=0.0,
        buy_fee=0.0, sell_fee=0.0, reduce_only=True,
    )
    execution = {
        "ok": True, "matched_qty": 0.0, "plan": exit_plan,
        "direction": "buy_entropy", "settled_ts": __import__("time").time(),
    }

    eng._update_rolling_position(execution, {
        "status": "unhedgeable", "filled_qty": 0.0,
    })
    assert eng._rolling_open_qty == 1.0

    eng._update_rolling_position(execution, {
        "status": "filled", "filled_qty": 1.0,
    })
    assert eng._rolling_open_direction is None
    assert eng._rolling_open_qty == 0.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
