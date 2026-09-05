"""Engine signal math: midline band directions, inventory ladder, scan.

Run:  python3 -m pytest tests/  (or  python3 tests/test_engine.py)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook, plan_arb  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
SELL_A_BUY_B = "sell_a_buy_b"
BUY_A_SELL_B = "buy_a_sell_b"


def make_cfg(midline=5.0, upper=4.0, lower=3.0, persist=0.0):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: {midline}
  upper_bps: {upper}
  lower_bps: {lower}
execution:
  premium_persist_sec: {persist}
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


def make_engine(**thr):
    cfg = make_cfg(**thr)
    eng = Engine(cfg)
    eng.venue_a = StubVenue("venue_a", "ENTROPY")
    eng.venue_b = StubVenue("venue_b", "RH")
    eng.venues = {"venue_a": eng.venue_a, "venue_b": eng.venue_b}
    eng._step, eng._min_base, eng._min_notional = 1e-4, 1e-4, 10.0
    return eng


def approx(a, b, tol=1e-9):
    assert abs(a - b) <= tol, f"{a} != {b}"


def test_engine_initializes_generic_pair_roles():
    eng = Engine(make_cfg())
    assert eng.venue_a is None
    assert eng.venue_b is None
    assert eng._armed == {SELL_A_BUY_B: None, BUY_A_SELL_B: None}


def test_eff_threshold_directions():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    a, b = eng.venue_a, eng.venue_b
    # sell entropy: hurdle = midline + upper = 9
    approx(eng._eff_threshold(buy=b, sell=a), 9.0)
    # buy entropy: hurdle = lower - midline = -2 (unwind side of a positive
    # midline is deliberately cheap — that's what completes the round trip)
    approx(eng._eff_threshold(buy=a, sell=b), -2.0)
    # round trip nets upper + lower regardless of midline sign
    for m in (-7.0, 0.0, 12.5):
        eng.cfg.midline_bps = m
        total = eng._eff_threshold(buy=b, sell=a) + eng._eff_threshold(buy=a, sell=b)
        approx(total, 7.0)


def test_inventory_ladder():
    eng = make_engine()
    eng.cfg.inventory_scale_bps, eng.cfg.inventory_floor_frac = 10.0, 0.5
    a, b = eng.venue_a, eng.venue_b
    a.set_book(99.9, 100.1)   # mid 100
    b.set_book(99.9, 100.1)
    approx(eng._inv_add_bps(a, b), 0.0)          # flat: dead zone
    a.position = 90.0                             # long $9k of $10k cap
    v = eng._inv_add_bps(a, b)                    # buying venue A adds long
    assert 7.5 < v < 8.5, v                       # u=0.9 -> ~+8
    approx(eng._inv_add_bps(b, a), 0.0)           # selling venue A reduces
    b.position = -90.0                            # venue B short $9k too
    v2 = eng._inv_add_bps(a, b)                   # both legs add -> max()
    assert abs(v2 - v) < 0.6, (v, v2)             # max, not sum


def test_premium_uses_venue_a_as_numerator():
    eng = make_engine()
    eng.venue_a.set_book(100.04, 100.06)
    eng.venue_b.set_book(99.99, 100.01)
    approx(eng.premium_bps(), 5.0, tol=1e-6)


def test_plan_keeps_original_executable_a_b_orientation():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    a, b = eng.venue_a, eng.venue_b
    a.set_book(100.14, 100.16)
    b.set_book(99.99, 100.01)
    expected, reason = plan_arb(
        b.book, a.book,
        threshold_bps=eng._eff_threshold(b, a),
        buy_fee_bps=b.fee_bps, sell_fee_bps=a.fee_bps,
        take_fraction=eng.cfg.take_fraction,
        cap_notional=eng.cfg.max_order_notional,
        min_base=eng._min_base, min_notional=eng._min_notional,
        size_step=eng._step)
    actual, actual_reason = eng._plan(b, a, eng.cfg.max_order_notional)
    assert reason == "ok"
    assert actual_reason == "ok"
    assert actual is not None and expected is not None
    for field in ("qty", "buy_limit", "sell_limit", "buy_notional",
                  "sell_notional", "marginal_premium_bps", "exp_edge_usd"):
        approx(getattr(actual, field), getattr(expected, field), tol=1e-9)


def run_scan(eng):
    async def go():
        # first pass arms the direction, second passes the persistence gate
        # (premium_persist_sec is 0 in the test config)
        eng._scan(__import__("time").time())
        return eng._scan(__import__("time").time())
    return asyncio.run(go())


def test_scan_fires_sell_a_buy_b_above_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # venue A (Entropy) 15 bps rich vs venue B (RH): above +9 -> sell A
    eng.venue_a.set_book(100.14, 100.16)
    eng.venue_b.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert sell is eng.venue_a and buy is eng.venue_b
    assert plan.exp_edge_usd > 0


def test_scan_quiet_inside_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # venue A 5 bps rich = exactly on the midline: inside the band
    eng.venue_a.set_book(100.04, 100.06)
    eng.venue_b.set_book(99.99, 100.01)
    assert run_scan(eng) is None


def test_scan_fires_buy_a_sell_b_below_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # venue A 5 bps CHEAP: below midline-lower=+2 -> buy A
    eng.venue_a.set_book(99.94, 99.96)
    eng.venue_b.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan = best
    assert buy is eng.venue_a and sell is eng.venue_b


def test_scan_waits_for_persistence_before_firing():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0, persist=1.0)
    eng.venue_a.set_book(100.14, 100.16)
    eng.venue_b.set_book(99.99, 100.01)

    async def go():
        assert eng._scan(100.0) is None
        assert eng._armed[SELL_A_BUY_B] == 100.0
        assert eng._scan(100.5) is None
        return eng._scan(101.0)

    best = asyncio.run(go())
    assert best is not None
    assert best[0] is eng.venue_b and best[1] is eng.venue_a


def test_scan_respects_position_caps():
    eng = make_engine(midline=0.0, upper=1.0, lower=1.0)
    eng.venue_a.set_book(100.14, 100.16)
    eng.venue_b.set_book(99.99, 100.01)
    eng.venue_a.position = -100.0   # venue A already short at its cap
    eng.venue_a.cap_usd = 10000.0
    eng.venue_b.position = 100.0
    eng.venue_b.cap_usd = 10000.0
    assert run_scan(eng) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
