"""Engine signal math: midline band directions, inventory ladder, scan.

Run:  python3 -m pytest tests/  (or  python3 tests/test_engine.py)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook, plan_arb  # noqa: E402
from entropy_arb.config import HLCreds, LighterCreds, load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
SELL_A_BUY_B = "sell_a_buy_b"
BUY_A_SELL_B = "buy_a_sell_b"


def make_cfg(midline=5.0, upper=4.0, lower=3.0, persist=0.0,
             venue_a="entropy", venue_b="lighter-rh", extra=""):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: {midline}
  upper_bps: {upper}
  lower_bps: {lower}
execution:
  premium_persist_sec: {persist}
{extra}
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", venue_a=venue_a, venue_b=venue_b)


class StubVenue:
    def __init__(self, key, label, cap=10000.0, fee=0.0):
        self.key, self.name = key, label
        self.cap_usd = cap
        self.fee_source = "configured"
        self.effective_taker_fee_bps = fee
        self.fee_bps = fee
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
    # Keep the original math fixture's $10k cap while sourcing fees from the
    # selected venue configs.
    eng.venue_a = StubVenue("venue_a", cfg.venue_a.label,
                            cap=10000.0, fee=cfg.venue_a.fee_bps)
    eng.venue_b = StubVenue("venue_b", cfg.venue_b.label,
                            cap=10000.0, fee=cfg.venue_b.fee_bps)
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
    # sell venue A: hurdle = midline + upper = 9
    approx(eng._eff_threshold(buy=b, sell=a), 9.0)
    # buy venue A: hurdle = lower - midline = -2 (unwind side of a positive
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


def test_non_entropy_pair_uses_the_same_role_math():
    eng = make_engine(venue_a="lighter-rh", venue_b="tradexyz")
    eng.venue_a.set_book(100.14, 100.16)
    eng.venue_b.set_book(99.99, 100.01)
    approx(eng.premium_bps(), 15.0, tol=1e-6)
    best = run_scan(eng)
    assert best is not None
    buy, sell, _ = best
    assert buy is eng.venue_b and sell is eng.venue_a


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


def test_plan_uses_effective_taker_fee_not_stale_configured_fee():
    eng = make_engine(midline=0.0, upper=4.0, lower=4.0)
    a, b = eng.venue_a, eng.venue_b
    a.fee_bps, a.effective_taker_fee_bps = 2.25, 4.5
    b.fee_bps, b.effective_taker_fee_bps = 0.0, 0.0
    a.set_book(100.19, 100.20)
    b.set_book(100.00, 100.01)

    expected, reason = plan_arb(
        b.book, a.book,
        threshold_bps=eng._eff_threshold(b, a),
        buy_fee_bps=b.effective_taker_fee_bps,
        sell_fee_bps=a.effective_taker_fee_bps,
        take_fraction=eng.cfg.take_fraction,
        cap_notional=eng.cfg.max_order_notional,
        min_base=eng._min_base, min_notional=eng._min_notional,
        size_step=eng._step)
    actual, actual_reason = eng._plan(b, a, eng.cfg.max_order_notional)
    assert reason == actual_reason == "ok"
    assert actual is not None and expected is not None
    approx(actual.sell_fee, 4.5 / 1e4)
    approx(actual.sell_fee, expected.sell_fee)
    approx(actual.exp_edge_usd, expected.exp_edge_usd)


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


class LifecycleVenue:
    def __init__(self, key, name, kind, conf):
        self.key, self.name, self.kind, self.conf = key, name, kind, conf
        self.address = f"address-{key}"
        self.signer_calls = 0
        self.shared_with = []
        self.query_address_calls = 0
        self.position = self.cash = self.volume_usd = 0.0
        self.last_traded_ts = __import__("time").time()
        self.size_decimals, self.min_base, self.min_quote = 4, 1e-4, 10.0
        self.cap_usd = 1000.0
        self.fee_source = conf.fee_source
        self.effective_taker_fee_bps = 0.0
        self.fee_bps = 0.0
        self.orders_per_min = 30
        self.equity = self.free = self.start_equity = None
        self.include_core_equity = True
        self.book = OrderBook()

    async def load_market(self):
        return None

    def init_signer(self):
        self.signer_calls += 1

    def share_nonces_with(self, other):
        self.shared_with.append(other)

    def _query_address(self):
        self.query_address_calls += 1
        return self.address

    def start_tasks(self, stop, on_update, live):
        return []

    async def close(self):
        return None


class LighterLifecycleVenue(LifecycleVenue):
    def __getattribute__(self, name):
        if name in {"share_nonces_with", "_query_address"}:
            raise AttributeError(name)
        return super().__getattribute__(name)


def test_only_hl_pair_uses_shared_hyperliquid_lifecycle(monkeypatch):
    cfg = make_cfg(venue_a="entropy", venue_b="tradexyz")
    cfg.venue_a.hl_creds = HLCreds("test-key", None)
    cfg.venue_b.hl_creds = HLCreds("test-key", None)
    cfg.recorder_enabled = False
    a = LifecycleVenue("venue_a", "ENTROPY", "hl", cfg.venue_a)
    b = LifecycleVenue("venue_b", "XYZ", "hl", cfg.venue_b)
    a.address = b.address = "shared-account"
    eng = Engine(cfg)
    monkeypatch.setattr(eng, "_make_venue",
                        lambda vc: a if vc.venue_name == "entropy" else b)
    eng.stop.set()
    asyncio.run(eng._run_inner())
    assert a.signer_calls == b.signer_calls == 1
    assert b in a.shared_with
    assert a.query_address_calls > 0 and b.query_address_calls > 0
    assert b.include_core_equity is False


def test_lighter_plus_hl_does_not_call_hl_only_methods(monkeypatch):
    cfg = make_cfg(venue_a="lighter-rh", venue_b="tradexyz")
    cfg.venue_a.lighter_creds = LighterCreds(1, 1, "test-key")
    cfg.venue_b.hl_creds = HLCreds("test-key", None)
    cfg.recorder_enabled = False
    # The real Lighter adapter has no Hyperliquid-only methods. Deliberately
    # omit them from this fake so an accidental call fails immediately.
    a = LighterLifecycleVenue("venue_a", "RH", "lighter", cfg.venue_a)
    b = LifecycleVenue("venue_b", "XYZ", "hl", cfg.venue_b)
    eng = Engine(cfg)
    monkeypatch.setattr(eng, "_make_venue",
                        lambda vc: a if vc.venue_name == "lighter-rh" else b)
    eng.stop.set()
    asyncio.run(eng._run_inner())
    assert b.shared_with == []
    assert a.signer_calls == b.signer_calls == 1
    assert b.include_core_equity is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
