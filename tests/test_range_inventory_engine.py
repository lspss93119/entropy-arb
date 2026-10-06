"""Range integration uses real Engine and fake transport only; zero API calls."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from entropy_arb.config import load_config
from entropy_arb.engine import Engine
from entropy_arb.range_inventory_live import CANARY_PARAMS, RangeInventoryLive
from test_engine import NO_ENV, StubVenue
from test_range_inventory_shadow import row


def make_range(tmp_path, monkeypatch):
    yaml = tmp_path / "config-test.yaml"
    yaml.write_text("""strategy:
  mode: range_inventory
thresholds:
  midline_bps: 99999
  upper_bps: 99999
  lower_bps: 99999
entropy:
  max_position_usd: 1500
hedge:
  max_position_usd: 1500
sizing:
  max_order_notional_usd: 90
  min_order_notional_usd: 10
execution:
  premium_persist_sec: 0
  cooldown_sec: 0
  staleness_sec: 3
  leg_slippage_bps: 10
""")
    cfg = load_config(str(yaml), NO_ENV, symbol="ANTH", hedge_venue="lighter-rh")
    cfg.trades_csv = str(tmp_path / "logs/trades/trades-ANTH-lighter-rh.csv")
    eng = Engine(cfg)
    assert eng._range is not None
    # Small windows are fixture-only. The engine's default frozen canary is
    # covered by the production-profile parity runner.
    eng._range = RangeInventoryLive(eng._range.path, symbol="ANTH", hedge="lighter-rh",
        params=replace(CANARY_PARAMS, long_window_minutes=4,
                       short_window_minutes=50, range_gate_window_minutes=4,
                       min_coverage_pct=100))
    e, h = StubVenue("entropy", "ENTROPY", cap=1500), StubVenue("hedge", "RH", cap=1500)
    eng.entropy, eng.hedge = e, h
    eng.venues = {"entropy": e, "hedge": h}
    eng._step, eng._min_base, eng._min_notional = .001, .001, 10
    clock = {"now": 3061.0}
    monkeypatch.setattr("entropy_arb.engine.time.time", lambda: clock["now"])
    for venue in (e, h):
        venue.set_book(99.9, 100.1, sz=100)
    calls = []
    async def send(venue, **kwargs):
        # Verify reservation exists durably before either transport receives it.
        saved = json.loads(open(eng._range.path).read())
        assert saved["pending_intent"]["qty"] == kwargs["qty"]
        calls.append((venue.key, kwargs))
        return {"status": "filled", "filled_base": kwargs["qty"],
                "avg_px": 100, "err": None, "unresolved": False}
    for venue in (e, h):
        async def send_taker(_venue=venue, **kwargs):
            return await send(_venue, **kwargs)
        venue.send_taker = send_taker
    eng._check_range_start_state()
    for i in range(50):
        eng._range.core.warmup_row(row(i, i))
    return eng, calls, clock


def test_completed_minute_wakes_immediate_real_execution(tmp_path, monkeypatch):
    eng, calls, _ = make_range(tmp_path, monkeypatch)
    eng._on_minute(row(50, -20))
    assert eng._update_evt.is_set()
    asyncio.run(eng._evaluate())
    assert len(calls) == 2
    assert eng.trades == 1
    assert eng._range.signed_qty == .528
    assert eng._rolling is None
    assert eng._rolling_ledger is None
    assert not eng.halted
    asyncio.run(eng._evaluate())
    assert len(calls) == 2  # $53/minute, no same-signal retry


@pytest.mark.parametrize("blocked", ["stale", "halt", "stop", "partial_minute"])
def test_no_orders_for_stale_halt_or_shutdown(tmp_path, monkeypatch, blocked):
    eng, calls, clock = make_range(tmp_path, monkeypatch)
    eng._on_minute(row(50, -20))
    if blocked == "stale":
        clock["now"] = 3076
    elif blocked == "halt":
        eng._set_halted("operator safety", source="operator")
    elif blocked == "stop":
        eng.request_stop()
    else:
        eng._range.signal = None
        eng._on_minute(row(51, -30))
    asyncio.run(eng._evaluate())
    assert calls == []


def test_final_submit_boundary_rechecks_signal_age(tmp_path, monkeypatch):
    eng, calls, clock = make_range(tmp_path, monkeypatch)
    eng._on_minute(row(50, -20))
    selected = eng._scan(clock["now"])
    assert selected is not None
    buy, sell, planned = selected
    clock["now"] = 3076
    async def go():
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy, sell, planned)
    asyncio.run(go())
    assert calls == []
    assert eng._range.signed_qty == 0
    assert not eng.halted


def test_mismatch_persists_halt_and_never_guesses_ledger_repair(tmp_path, monkeypatch):
    eng, calls, _ = make_range(tmp_path, monkeypatch)
    eng.entropy.position, eng.hedge.position = .1, -.1
    with pytest.raises(RuntimeError, match="mismatch"):
        eng._check_range_start_state()
    assert eng.halted
    assert eng._range.signed_qty == 0
    assert calls == []
    assert json.loads(open(eng._halt_state_path()).read())["halted"]


def test_persisted_production_halt_cannot_be_bypassed_by_range(tmp_path, monkeypatch):
    eng, calls, _ = make_range(tmp_path, monkeypatch)
    eng._set_halted("existing production incident", source="reconciliation")
    other = Engine(eng.cfg)
    assert other.halted
    assert other._range.path != eng._halt_state_path()
    eng._on_minute(row(50, -20))
    asyncio.run(eng._evaluate())
    assert calls == []


def test_range_reduction_ignores_rolling_threshold_and_lot_be(tmp_path, monkeypatch):
    eng, calls, clock = make_range(tmp_path, monkeypatch)
    eng._on_minute(row(50, -20))
    asyncio.run(eng._evaluate())
    before = eng._range.signed_qty
    clock["now"] = 3121
    for venue in eng.venues.values():
        venue.set_book(99.9, 100.1, sz=100)
    eng._on_minute(row(51, 100))
    asyncio.run(eng._evaluate())
    assert len(calls) == 4
    assert all(kwargs["reduce_only"] for _, kwargs in calls[2:])
    assert calls[2][1]["qty"] == before
    assert eng._range.signed_qty == 0
    assert not eng.halted


def test_resume_requires_validated_range_state(tmp_path, monkeypatch):
    eng, _, _ = make_range(tmp_path, monkeypatch)
    eng._set_halted("operator", source="operator")
    eng.entropy.position = .1
    with pytest.raises(RuntimeError, match="mismatch"):
        eng.resume_after_reconcile()
    assert eng.halted
    eng.entropy.position = 0
    eng.resume_after_reconcile()
    assert not eng.halted


def test_runtime_mismatch_does_not_send_reconciliation_hedge(tmp_path, monkeypatch):
    eng, calls, _ = make_range(tmp_path, monkeypatch)
    async def entropy_position():
        return .1
    async def hedge_position():
        return 0
    eng.entropy.fetch_position = entropy_position
    eng.hedge.fetch_position = hedge_position
    asyncio.run(eng._reconcile_positions(hedge=True))
    assert eng.halted
    assert calls == []


def test_partial_primary_build_and_real_residual_pipeline(tmp_path, monkeypatch):
    eng, _, _ = make_range(tmp_path, monkeypatch)
    submitted = []
    async def entropy_send(**kw):
        submitted.append(("entropy", kw))
        qty = .2 if kw["reduce_only"] else .5
        return {"status": "filled", "filled_base": qty, "avg_px": 100,
                "err": None, "unresolved": False}
    async def rh_send(**kw):
        submitted.append(("hedge", kw))
        return {"status": "filled", "filled_base": .3, "avg_px": 100,
                "err": None, "unresolved": False}
    eng.entropy.send_taker, eng.hedge.send_taker = entropy_send, rh_send
    eng._on_minute(row(50, -20))
    asyncio.run(eng._evaluate())
    assert len(submitted) == 3
    assert submitted[-1][1]["reduce_only"]
    assert eng.hedges == 1
    assert eng._range.signed_qty == pytest.approx(.3)
    assert eng.entropy.position == pytest.approx(.3)
    assert eng.hedge.position == pytest.approx(-.3)
    assert eng._range.state["pending_intent"] is None
    assert not eng.halted


def test_partial_reduce_and_residual_close_not_false_flat(tmp_path, monkeypatch):
    eng, _, clock = make_range(tmp_path, monkeypatch)
    eng._on_minute(row(50, -20))
    asyncio.run(eng._evaluate())
    before = eng._range.signed_qty
    clock["now"] = 3121
    for venue in eng.venues.values():
        venue.set_book(99.9, 100.1)
    rh_calls = []
    async def entropy_send(**kw):
        return {"status": "filled", "filled_base": .3, "avg_px": 101,
                "err": None, "unresolved": False}
    async def rh_send(**kw):
        rh_calls.append(kw)
        qty = .2 if len(rh_calls) == 1 else .1
        if len(rh_calls) == 2:
            assert eng._inflight_reconciliation is not None
            assert eng._inflight_reconciliation_explains({
                "entropy": before - .3, "hedge": -before + .2})
        return {"status": "filled", "filled_base": qty, "avg_px": 100,
                "err": None, "unresolved": False}
    eng.entropy.send_taker, eng.hedge.send_taker = entropy_send, rh_send
    eng._on_minute(row(51, 100))
    asyncio.run(eng._evaluate())
    assert len(rh_calls) == 2
    assert eng.hedges == 1
    assert eng._range.signed_qty == pytest.approx(before - .3)
    assert eng._range.state["cumulative_realized_capture_usd"] == pytest.approx(.3)
    assert not eng.halted


def test_unhedgeable_partial_fill_keeps_explicit_intent_and_halt(tmp_path, monkeypatch):
    eng, _, _ = make_range(tmp_path, monkeypatch)
    async def entropy_send(**kw):
        return {"status": "filled", "filled_base": .1, "avg_px": 100,
                "err": None, "unresolved": False}
    async def rh_send(**kw):
        return {"status": "filled", "filled_base": .09, "avg_px": 100,
                "err": None, "unresolved": False}
    eng.entropy.send_taker, eng.hedge.send_taker = entropy_send, rh_send
    eng._on_minute(row(50, -20))
    asyncio.run(eng._evaluate())
    assert eng.halted
    assert eng._range.signed_qty == 0
    assert eng._range.state["pending_intent"] is not None


def test_record_only_never_writes_range_state(tmp_path, monkeypatch):
    eng, calls, _ = make_range(tmp_path, monkeypatch)
    eng.record_only = True
    eng._on_minute(row(50, -20))
    assert eng._range.signal is None
    assert calls == []


def test_range_live_requires_recorder(tmp_path, monkeypatch):
    eng, _, _ = make_range(tmp_path, monkeypatch)
    eng.cfg.recorder_enabled = False
    with pytest.raises(RuntimeError, match="requires recorder"):
        eng._validate_rolling_runtime(live=True)


@pytest.mark.parametrize("change", ["stale_signal", "halt", "stop", "depth", "stale_bbo", "cap"])
def test_scheduler_gap_cannot_bypass_submission_guards(tmp_path, monkeypatch, change):
    eng, calls, clock = make_range(tmp_path, monkeypatch)
    eng._on_minute(row(50, -20))
    original_gather = asyncio.gather
    async def delayed(*coros, **kwargs):
        if change == "stale_signal":
            clock["now"] = 3076
        elif change == "halt":
            eng._set_halted("operator", source="operator")
        elif change == "stop":
            eng.request_stop()
        elif change == "depth":
            eng.hedge.book.bids = {99.9: .01}
        elif change == "stale_bbo":
            clock["now"] += 4
        else:
            eng.hedge.cap_usd = 5
        return await original_gather(*coros, **kwargs)
    monkeypatch.setattr("entropy_arb.engine.asyncio.gather", delayed)
    asyncio.run(eng._evaluate())
    assert calls == []
    assert eng._range.state["pending_intent"] is None  # proven zero submissions
    assert eng._range.signed_qty == 0


def test_persistence_delay_rechecks_books_before_send(tmp_path, monkeypatch):
    eng, calls, clock = make_range(tmp_path, monkeypatch)
    eng._on_minute(row(50, -20))
    reserve = eng._range.reserve
    def delayed(result, *, now):
        reserve(result, now=now)
        clock["now"] += 4  # signal still young, BBO now exceeds 3s freshness
    monkeypatch.setattr(eng._range, "reserve", delayed)
    asyncio.run(eng._evaluate())
    assert calls == []
    assert eng._range.state["pending_intent"] is None


def test_delayed_transport_keeps_guard_and_repairs_actual_one_leg_fill(tmp_path, monkeypatch):
    eng, _, clock = make_range(tmp_path, monkeypatch)
    eng._on_minute(row(50, -20))
    signed = []
    async def entropy_send(**kw):
        signed.append("entropy-primary" if not kw["reduce_only"] else "entropy-residual")
        return {"status": "filled", "filled_base": kw["qty"], "avg_px": 100,
                "err": None, "unresolved": False}
    async def rh_send(**kw):
        await asyncio.sleep(0)  # models await nonce after the other primary submitted
        clock["now"] += 4
        assert "submit_guard" in kw, "guard must reach the actual transport"
        assert not kw["submit_guard"]()
        for venue in eng.venues.values():
            venue.set_book(99.9, 100.1, sz=100)  # fresh feed for existing safety hedge
        return {"status": "pre-submit-blocked", "filled_base": 0,
                "avg_px": None, "err": None, "unresolved": False, "not_submitted": True}
    eng.entropy.send_taker, eng.hedge.send_taker = entropy_send, rh_send
    asyncio.run(eng._evaluate())
    assert signed == ["entropy-primary", "entropy-residual"]
    assert eng._range.signed_qty == 0
    assert eng._range.state["pending_intent"] is None
    assert not eng.halted


def test_new_minute_cannot_bypass_existing_persist_arming(tmp_path, monkeypatch):
    eng, calls, clock = make_range(tmp_path, monkeypatch)
    eng._on_minute(row(50, -20))
    selected = eng._scan(clock["now"])
    eng.cfg.premium_persist_sec = .5
    clock["now"] = 3121
    for venue in eng.venues.values():
        venue.set_book(99.9, 100.1, sz=100)
    eng._on_minute(row(51, -30))
    buy, sell, planned = selected
    async def go():
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy, sell, planned)
    asyncio.run(go())
    assert calls == []
    assert eng._range.state["pending_intent"] is None


@pytest.mark.parametrize("allow", [False, True])
def test_hl_transport_guard_is_optional_and_before_post(allow):
    from entropy_arb.venue_hl import HLVenue, NonceAllocator
    venue = object.__new__(HLVenue)
    venue.account = SimpleNamespace(nonces=NonceAllocator(), wallet=None, is_mainnet=True)
    venue.asset_id, venue.coin = 1, "ANTH"
    venue._next_cloid = lambda: "test-only-cloid"
    venue._signing = SimpleNamespace(order_request_to_order_wire=lambda req, asset: req,
        order_wires_to_order_action=lambda wires: wires, sign_l1_action=lambda *args: "test-only")
    posted = []
    async def post(payload):
        posted.append(payload)
        return {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": ".2", "avgPx": "100", "oid": 1}},
        ]}}}, None, False
    venue._post_exchange = post
    kwargs = {} if allow else {"submit_guard": lambda: False}
    result = asyncio.run(venue.send_taker(is_buy=True, qty=.2, limit_px=100, **kwargs))
    if allow:
        assert len(posted) == 1
        assert result["filled_base"] == .2
    else:
        assert posted == []
        assert result["not_submitted"] is True
