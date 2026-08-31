"""Engine signal math: midline band directions, inventory ladder, scan.

Run:  python3 -m pytest tests/  (or  python3 tests/test_engine.py)
"""
import asyncio
import logging
import os
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from entropy_arb.premium import calculate_premiums  # noqa: E402

NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def make_cfg(
    midline=5.0,
    upper=4.0,
    lower=3.0,
    *,
    hedge_venue="lighter-rh",
    recorder_enabled=True,
    strategy_name="stable_basis",
    window_minutes=60,
):
    if strategy_name == "stable_basis":
        strategy_yaml = f"""
strategy:
  name: stable_basis
  params:
    center_bps: {midline}
    upper_bps: {upper}
    lower_bps: {lower}
"""
    elif strategy_name == "drifting_basis":
        strategy_yaml = f"""
strategy:
  name: drifting_basis
  params:
    window_minutes: {window_minutes}
    upper_bps: {upper}
    lower_bps: {lower}
"""
    else:
        raise ValueError(strategy_name)

    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(strategy_yaml + f"""
execution:
  premium_persist_sec: 0.0
recorder:
  enabled: {str(recorder_enabled).lower()}
  csv: {os.path.join(tempfile.gettempdir(), "engine-minutes.csv")}
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", hedge_venue=hedge_venue)


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
    state = eng.strategy.state()
    # sell entropy: hurdle = midline + upper = 9
    approx(eng._eff_threshold(buy=h, sell=e, state=state), 9.0)
    # buy entropy: hurdle = lower - midline = -2 (unwind side of a positive
    # midline is deliberately cheap — that's what completes the round trip)
    approx(eng._eff_threshold(buy=e, sell=h, state=state), -2.0)
    # round trip nets upper + lower regardless of midline sign
    for m in (-7.0, 0.0, 12.5):
        eng = make_engine(midline=m, upper=4.0, lower=3.0)
        state = eng.strategy.state()
        total = (
            eng._eff_threshold(buy=eng.hedge, sell=eng.entropy, state=state)
            + eng._eff_threshold(buy=eng.entropy, sell=eng.hedge, state=state)
        )
        approx(total, 7.0)


def test_stable_strategy_preserves_legacy_hurdle_math():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    state = eng.strategy.state()
    e, h = eng.entropy, eng.hedge
    approx(eng._eff_threshold(h, e, state), 9.0)
    approx(eng._eff_threshold(e, h, state), -2.0)
    approx(
        eng._eff_threshold(h, e, state)
        + eng._eff_threshold(e, h, state),
        7.0,
    )


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
        eng._scan(time.time())
        return eng._scan(time.time())
    return asyncio.run(go())


async def run_one_status_cycle(eng):
    eng.cfg.status_interval_sec = 0.01
    task = asyncio.create_task(eng._status_loop())
    await asyncio.sleep(0.02)
    eng.stop.set()
    await task


def test_scan_fires_sell_entropy_above_band():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    # entropy 15 bps rich vs hedge: above midline+upper=9 -> sell entropy
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    best = run_scan(eng)
    assert best is not None
    buy, sell, plan, state = best
    assert state.center_bps == 5.0
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
    buy, sell, plan, state = best
    assert state.center_bps == 5.0
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


def test_unready_drifting_strategy_blocks_scan_and_clears_arming():
    eng = make_engine(
        upper=4.0,
        lower=3.0,
        strategy_name="drifting_basis",
        window_minutes=60,
    )
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng._armed["sell_entropy"] = 111.0
    eng._armed["buy_entropy"] = 222.0

    assert eng._scan(time.time()) is None
    assert eng._armed == {
        "sell_entropy": None,
        "buy_entropy": None,
    }


def test_stable_strategy_observer_helper_is_noop():
    eng = make_engine(strategy_name="stable_basis")
    assert eng.strategy.requires_observations is False
    eng.entropy.set_book(100.10, 100.20)
    eng.hedge.set_book(99.90, 100.00)

    before = eng.strategy.state()
    assert eng._sample_strategy_observation(now=1000.0) is False
    assert eng.strategy.state() == before
    assert not eng._update_evt.is_set()


def test_drifting_observation_uses_mid_premium_and_fresh_books():
    eng = make_engine(strategy_name="drifting_basis", window_minutes=1)
    eng.entropy.set_book(100.10, 100.20)
    eng.hedge.set_book(99.90, 100.00)

    assert eng._sample_strategy_observation(now=1000.0) is True
    state = eng.strategy.state()
    assert state.ready is False
    assert state.center_bps is None
    assert state.coverage_ratio > 0
    assert eng._update_evt.is_set()


def test_drifting_observation_rejects_unready_empty_and_stale_books():
    eng = make_engine(strategy_name="drifting_basis", window_minutes=1)
    before = eng.strategy.state()

    assert eng._sample_strategy_observation(now=1000.0) is False

    eng.entropy.set_book(100.10, 100.20)
    eng.hedge.book.ready = True
    eng.hedge.book.touch()
    assert eng._sample_strategy_observation(now=1001.0) is False

    eng.hedge.set_book(99.90, 100.00)
    eng.entropy.book.alive_ts = time.time() - eng.cfg.staleness_sec - 1.0
    assert eng._sample_strategy_observation(now=1002.0) is False

    assert eng.strategy.state() == before
    assert not eng._update_evt.is_set()


def test_engine_premium_bps_matches_shared_helper():
    eng = make_engine()
    eng.entropy.set_book(100.10, 100.20)
    eng.hedge.set_book(99.90, 100.00)

    values = calculate_premiums(
        entropy_bid=100.10,
        entropy_ask=100.20,
        hedge_bid=99.90,
        hedge_ask=100.00,
    )
    assert eng.premium_bps() == pytest.approx(values.premium_bps)


def test_drifting_observations_reach_ready_from_live_books():
    eng = make_engine(strategy_name="drifting_basis", window_minutes=1)
    eng.entropy.set_book(100.10, 100.20)
    eng.hedge.set_book(99.90, 100.00)

    for offset in range(61):
        assert eng._sample_strategy_observation(now=1000.0 + offset) is True

    state = eng.strategy.state()
    values = calculate_premiums(
        entropy_bid=100.10,
        entropy_ask=100.20,
        hedge_bid=99.90,
        hedge_ask=100.00,
    )
    assert state.ready is True
    assert state.center_bps == pytest.approx(values.premium_bps)


def test_strategy_observation_loop_cancels_cleanly():
    async def scenario():
        eng = make_engine(strategy_name="drifting_basis", window_minutes=1)
        samples = 0

        def fake_sample(now=None):
            nonlocal samples
            samples += 1
            return True

        eng._sample_strategy_observation = fake_sample
        task = asyncio.create_task(eng._strategy_observation_loop())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert samples == 1

    asyncio.run(scenario())


def test_status_reports_stable_strategy(caplog):
    eng = make_engine(strategy_name="stable_basis", midline=-1.0,
                      upper=3.0, lower=3.5)
    eng.entropy.set_book(100.0, 100.1)
    eng.hedge.set_book(100.0, 100.1)

    caplog.set_level(logging.INFO, logger="engine")
    asyncio.run(run_one_status_cycle(eng))

    assert "strategy=stable_basis" in caplog.text
    assert "center=-1.00" in caplog.text


def test_status_reports_drifting_warmup_without_mutation(caplog):
    eng = make_engine(strategy_name="drifting_basis", window_minutes=1)
    eng.entropy.set_book(100.0, 100.1)
    eng.hedge.set_book(100.0, 100.1)
    eng.strategy.update(1000.0, 0.0)
    before = eng.strategy.state()

    caplog.set_level(logging.INFO, logger="engine")
    asyncio.run(run_one_status_cycle(eng))

    after = eng.strategy.state()
    assert "strategy=drifting_basis" in caplog.text
    assert "WARMING_UP" in caplog.text
    assert before == after


def test_log_csv_records_captured_strategy_center(tmp_path):
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    state = eng.strategy.state()
    trades_csv = tmp_path / "trades.csv"
    eng.cfg.trades_csv = str(trades_csv)
    plan = SimpleNamespace(
        qty=1.25,
        buy_limit=99.5,
        sell_limit=100.5,
        buy_notional=124.38,
        sell_notional=125.62,
        exp_edge_usd=1.23,
        gross_edge_usd=1.56,
        marginal_premium_bps=12.345,
    )
    captured = SimpleNamespace(
        ready=True,
        center_bps=7.5,
        upper_bps=state.upper_bps,
        lower_bps=state.lower_bps,
    )

    eng._log_csv(
        "sell_entropy",
        eng.hedge,
        eng.entropy,
        plan,
        True,
        1.25,
        1.25,
        "filled",
        "filled",
        1.11,
        0.0,
        captured,
    )

    rows = trades_csv.read_text().strip().splitlines()
    assert rows[0].split(",")[12] == "midline_bps"
    assert rows[1].split(",")[12] == "7.500"


def test_run_inner_logs_selected_stable_strategy_and_no_auto_selection(
        monkeypatch, caplog):
    from entropy_arb import engine as engine_module

    class LifecycleVenue:
        def __init__(self, key, name):
            self.kind = "hl"
            self.key = key
            self.name = name
            self.conf = SimpleNamespace(symbol="SNDK")
            self.size_decimals = 4
            self.min_base = 0.0001
            self.min_quote = 10.0
            self.fee_bps = 0.0
            self.book = OrderBook()
            self.position = 0.0
            self.orders_per_min = 30

        async def load_market(self):
            return None

        def start_tasks(self, stop, notify, live):
            return []

        async def close(self):
            return None

        def _query_address(self):
            return None

    class QuietMinuteRecorder:
        rows_written = 0

        def __init__(self, *args, **kwargs):
            return None

        async def run(self, stop):
            await stop.wait()

    async def scenario():
        cfg = make_cfg(
            midline=-1.0,
            upper=3.0,
            lower=3.5,
            hedge_venue="tradexyz",
            recorder_enabled=False,
        )
        eng = Engine(cfg, record_only=True)
        venues = iter([
            LifecycleVenue("entropy", "ENTROPY"),
            LifecycleVenue("hedge", "XYZ"),
        ])
        monkeypatch.setattr(eng, "_make_venue", lambda conf: next(venues))
        monkeypatch.setattr(engine_module, "MinuteRecorder", QuietMinuteRecorder)
        run_task = asyncio.create_task(eng._run_inner())
        await asyncio.sleep(0)
        eng.request_stop()
        await asyncio.wait_for(run_task, timeout=1.0)

    caplog.set_level(logging.INFO, logger="engine")
    asyncio.run(scenario())
    assert "strategy=stable_basis center=-1.00bps band=[-4.50,+2.00]" in caplog.text
    assert "No automatic strategy selection." in caplog.text


def test_run_inner_logs_drifting_warmup_strategy_and_no_auto_selection(
        monkeypatch, caplog):
    from entropy_arb import engine as engine_module

    class LifecycleVenue:
        def __init__(self, key, name):
            self.kind = "hl"
            self.key = key
            self.name = name
            self.conf = SimpleNamespace(symbol="SNDK")
            self.size_decimals = 4
            self.min_base = 0.0001
            self.min_quote = 10.0
            self.fee_bps = 0.0
            self.book = OrderBook()
            self.position = 0.0
            self.orders_per_min = 30

        async def load_market(self):
            return None

        def start_tasks(self, stop, notify, live):
            return []

        async def close(self):
            return None

        def _query_address(self):
            return None

    class QuietMinuteRecorder:
        rows_written = 0

        def __init__(self, *args, **kwargs):
            return None

        async def run(self, stop):
            await stop.wait()

    async def scenario():
        cfg = make_cfg(
            upper=3.0,
            lower=3.5,
            hedge_venue="tradexyz",
            recorder_enabled=False,
            strategy_name="drifting_basis",
            window_minutes=60,
        )
        eng = Engine(cfg, record_only=True)
        venues = iter([
            LifecycleVenue("entropy", "ENTROPY"),
            LifecycleVenue("hedge", "XYZ"),
        ])
        monkeypatch.setattr(eng, "_make_venue", lambda conf: next(venues))
        monkeypatch.setattr(engine_module, "MinuteRecorder", QuietMinuteRecorder)
        run_task = asyncio.create_task(eng._run_inner())
        await asyncio.sleep(0)
        eng.request_stop()
        await asyncio.wait_for(run_task, timeout=1.0)

    caplog.set_level(logging.INFO, logger="engine")
    asyncio.run(scenario())
    assert ("strategy=drifting_basis window=60m center=WARMING_UP "
            "band-offset=[-3.50,+3.00]") in caplog.text
    assert "No automatic strategy selection." in caplog.text


class LifecycleVenue:
    def __init__(self, key, name, *, kind="hl"):
        self.kind = kind
        self.key = key
        self.name = name
        self.conf = SimpleNamespace(symbol="SNDK")
        self.size_decimals = 4
        self.min_base = 0.0001
        self.min_quote = 10.0
        self.fee_bps = 0.0
        self.book = OrderBook()
        self.position = 0.0
        self.orders_per_min = 30

    async def load_market(self):
        return None

    def start_tasks(self, stop, notify, live):
        return []

    async def close(self):
        return None

    def _query_address(self):
        return None

    def init_signer(self):
        return None

    def share_nonces_with(self, other):
        return None


async def run_strategy_wiring_scenario(
    monkeypatch,
    *,
    strategy_name,
    record_only,
):
    cfg = make_cfg(
        strategy_name=strategy_name,
        window_minutes=1,
        hedge_venue="tradexyz",
        recorder_enabled=False,
    )
    if not record_only:
        monkeypatch.setattr(type(cfg), "creds_complete", property(lambda self: True))
    eng = Engine(cfg, record_only=record_only)
    venues = iter([
        LifecycleVenue("entropy", "ENTROPY"),
        LifecycleVenue("hedge", "XYZ"),
    ])
    monkeypatch.setattr(eng, "_make_venue", lambda conf: next(venues))

    started = {
        "strategy": asyncio.Event(),
        "observer": asyncio.Event(),
    }

    async def strategy_loop():
        started["strategy"].set()
        await eng.stop.wait()

    async def observer_loop():
        started["observer"].set()
        await eng.stop.wait()

    async def idle_loop():
        await eng.stop.wait()

    async def fake_reconcile_positions(*args, **kwargs):
        return None

    monkeypatch.setattr(eng, "_strategy_loop", strategy_loop)
    monkeypatch.setattr(
        eng,
        "_strategy_observation_loop",
        observer_loop,
        raising=False,
    )
    monkeypatch.setattr(eng, "_balance_loop", idle_loop)
    monkeypatch.setattr(eng, "_http_keepalive_loop", idle_loop)
    monkeypatch.setattr(eng, "_status_loop", idle_loop)
    monkeypatch.setattr(eng, "_reconcile_loop", idle_loop)
    monkeypatch.setattr(eng, "_reconcile_positions", fake_reconcile_positions)

    run_task = asyncio.create_task(eng._run_inner())
    for _ in range(20):
        await asyncio.sleep(0.01)
        if run_task.done() or started["strategy"].is_set() or started["observer"].is_set():
            break
    snapshot = {
        "strategy": started["strategy"].is_set(),
        "observer": started["observer"].is_set(),
    }
    eng.request_stop()
    await asyncio.wait_for(run_task, timeout=1.0)
    return snapshot


def test_live_stable_run_inner_starts_strategy_without_observer(monkeypatch):
    started = asyncio.run(
        run_strategy_wiring_scenario(
            monkeypatch,
            strategy_name="stable_basis",
            record_only=False,
        )
    )
    assert started == {
        "strategy": True,
        "observer": False,
    }


def test_live_drifting_run_inner_starts_strategy_and_observer(monkeypatch):
    started = asyncio.run(
        run_strategy_wiring_scenario(
            monkeypatch,
            strategy_name="drifting_basis",
            record_only=False,
        )
    )
    assert started == {
        "strategy": True,
        "observer": True,
    }


def test_record_only_drifting_run_inner_skips_strategy_and_observer(monkeypatch):
    started = asyncio.run(
        run_strategy_wiring_scenario(
            monkeypatch,
            strategy_name="drifting_basis",
            record_only=True,
        )
    )
    assert started == {
        "strategy": False,
        "observer": False,
    }


def attach_reference_venues(
    eng,
    *,
    market_id=32,
    hedge_ws_url="wss://api.rh.lighter.xyz/stream",
):
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.entropy.ws_url = "wss://api.hyperliquid.xyz/ws"
    eng.entropy.coin = "io:SNDK"
    eng.hedge = StubVenue("hedge", "RH")
    eng.hedge.kind = "lighter"
    eng.hedge.profile = SimpleNamespace(ws_url=hedge_ws_url)
    eng.hedge.market_id = market_id


@pytest.mark.parametrize(
    ("record_only", "recorder_enabled", "expected"),
    [
        (True, False, True),
        (False, True, True),
        (False, False, False),
    ],
)
def test_reference_lifecycle_matrix(
        monkeypatch, record_only, recorder_enabled, expected):
    from entropy_arb import engine as engine_module

    captured = []

    class SpyReferenceRecorder:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(
        engine_module, "ReferenceRecorder", SpyReferenceRecorder
    )
    eng = Engine(
        make_cfg(recorder_enabled=recorder_enabled),
        record_only=record_only,
    )
    attach_reference_venues(eng)
    recorder = eng._build_reference_recorder()
    assert (recorder is not None) is expected
    if expected:
        assert captured == [{
            "symbol": "SNDK",
            "hedge_key": "lighter-rh",
            "entropy_ws_url": "wss://api.hyperliquid.xyz/ws",
            "entropy_coin": "io:SNDK",
            "hedge_ws_url": "wss://api.rh.lighter.xyz/stream",
            "hedge_market_id": 32,
        }]


@pytest.mark.parametrize(
    ("hedge_key", "hedge_ws_url", "market_id"),
    [
        (
            "lighter",
            "wss://mainnet.zklighter.elliot.ai/stream",
            139,
        ),
        (
            "lighter-rh",
            "wss://api.rh.lighter.xyz/stream",
            32,
        ),
    ],
)
def test_reference_factory_uses_runtime_resolved_metadata(
        monkeypatch, hedge_key, hedge_ws_url, market_id):
    from entropy_arb import engine as engine_module

    captured = []

    class SpyReferenceRecorder:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(
        engine_module, "ReferenceRecorder", SpyReferenceRecorder
    )
    eng = Engine(
        make_cfg(hedge_venue=hedge_key, recorder_enabled=True)
    )
    attach_reference_venues(
        eng,
        market_id=market_id,
        hedge_ws_url=hedge_ws_url,
    )
    eng._build_reference_recorder()
    assert captured[0]["entropy_coin"] == "io:SNDK"
    assert captured[0]["hedge_key"] == hedge_key
    assert captured[0]["hedge_ws_url"] == hedge_ws_url
    assert captured[0]["hedge_market_id"] == market_id


def test_tradexyz_does_not_build_reference_collector():
    eng = Engine(
        make_cfg(hedge_venue="tradexyz", recorder_enabled=True),
        record_only=True,
    )
    eng.entropy = SimpleNamespace(
        ws_url="wss://api.hyperliquid.xyz/ws",
        coin="io:SNDK",
    )
    eng.hedge = SimpleNamespace(kind="hl")
    assert eng._build_reference_recorder() is None


def test_reference_factory_has_no_strategy_wakeup_dependency(monkeypatch):
    from entropy_arb import engine as engine_module

    captured = {}

    class SpyReferenceRecorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        engine_module, "ReferenceRecorder", SpyReferenceRecorder
    )
    eng = Engine(make_cfg(), record_only=True)
    attach_reference_venues(eng)
    eng.reference = eng._build_reference_recorder()
    assert "notify" not in captured
    assert "update_evt" not in captured
    assert not eng._update_evt.is_set()


def test_reference_failure_is_nonfatal_and_does_not_set_engine_events(caplog):
    class BrokenReference:
        async def run(self, stop):
            raise RuntimeError("reference failed")

    async def scenario():
        eng = Engine(make_cfg(), record_only=True)
        eng.reference = BrokenReference()
        await eng._run_reference()
        assert not eng.stop.is_set()
        assert not eng._update_evt.is_set()
        assert not eng._reconcile_evt.is_set()

    asyncio.run(scenario())
    assert "reference recorder failed" in caplog.text


def test_no_strategy_wakeup_after_successful_reference_run():
    class SuccessfulReference:
        async def run(self, stop):
            return None

    async def scenario():
        eng = Engine(make_cfg(), record_only=False)
        evaluations = 0

        async def tracked_evaluate():
            nonlocal evaluations
            evaluations += 1

        eng._evaluate = tracked_evaluate
        eng.reference = SuccessfulReference()
        strategy_task = asyncio.create_task(eng._strategy_loop())
        await asyncio.sleep(0)
        await eng._run_reference()
        await asyncio.sleep(0)
        assert not eng._update_evt.is_set()
        assert evaluations == 0
        eng.stop.set()
        eng._update_evt.set()
        await strategy_task

    asyncio.run(scenario())


def test_engine_awaits_reference_shutdown_without_cancelling_it(monkeypatch):
    from entropy_arb import engine as engine_module

    async def scenario():
        started = asyncio.Event()
        closed = asyncio.Event()
        was_cancelled = False

        class LifecycleVenue:
            def __init__(self, kind, *, coin=None, market_id=None, ws_url=None):
                self.kind = kind
                self.key = "entropy" if coin else "hedge"
                self.name = self.key.upper()
                self.conf = SimpleNamespace(symbol="SNDK")
                self.ws_url = ws_url
                self.coin = coin
                self.market_id = market_id
                self.profile = SimpleNamespace(ws_url=ws_url)
                self.size_decimals = 4
                self.min_base = 0.0001
                self.min_quote = 10.0
                self.fee_bps = 0.0
                self.book = OrderBook()

            async def load_market(self):
                return None

            def start_tasks(self, stop, notify, live):
                return []

            async def close(self):
                return None

        class SpyReferenceRecorder:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def run(self, stop):
                nonlocal was_cancelled
                started.set()
                try:
                    await stop.wait()
                    await asyncio.sleep(0)
                    closed.set()
                except asyncio.CancelledError:
                    was_cancelled = True
                    raise

        class QuietMinuteRecorder:
            rows_written = 0

            def __init__(self, *args, **kwargs):
                return None

            async def run(self, stop):
                await stop.wait()

        cfg = make_cfg(recorder_enabled=False)
        eng = Engine(cfg, record_only=True)
        venues = iter([
            LifecycleVenue(
                "hl",
                coin="io:SNDK",
                ws_url="wss://api.hyperliquid.xyz/ws",
            ),
            LifecycleVenue(
                "lighter",
                market_id=32,
                ws_url="wss://api.rh.lighter.xyz/stream",
            ),
        ])
        monkeypatch.setattr(eng, "_make_venue", lambda conf: next(venues))
        monkeypatch.setattr(
            engine_module, "ReferenceRecorder", SpyReferenceRecorder
        )
        monkeypatch.setattr(
            engine_module, "MinuteRecorder", QuietMinuteRecorder
        )
        run_task = asyncio.create_task(eng._run_inner())
        await asyncio.wait_for(started.wait(), timeout=0.2)
        eng.request_stop()
        await asyncio.wait_for(run_task, timeout=1.0)
        assert closed.is_set()
        assert was_cancelled is False

    asyncio.run(scenario())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
