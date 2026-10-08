"""Range integration uses real Engine and fake transport only; zero API calls."""
import asyncio
import csv
import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from entropy_arb.config import load_config
from entropy_arb.engine import Engine
from entropy_arb.range_inventory_live import (
    CANARY_PARAMS, RangeInventoryLive, RangeStateError)
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
        guard = kwargs.get("submit_guard")
        if guard is not None and not guard():
            result = guard.last_result
            return {"status": "pre-submit-blocked", "filled_base": 0.0,
                    "avg_px": None, "err": None, "unresolved": False,
                    "reason": f"range_guard:{result['reason']}",
                    "transport_attempted": False, "not_submitted": True,
                    "venue_guard_ts": "test-venue-guard"}
        calls.append((venue.key, kwargs))
        return {"status": "filled", "filled_base": kwargs["qty"],
                "avg_px": 100, "err": None, "unresolved": False,
                "transport_attempted": True,
                "venue_guard_ts": "test-venue-guard"}
    for venue in (e, h):
        async def send_taker(_venue=venue, **kwargs):
            return await send(_venue, **kwargs)
        venue.send_taker = send_taker
    eng._check_range_start_state()
    for i in range(50):
        eng._range.core.warmup_row(row(i, i))
    return eng, calls, clock


def prepared_range(eng, clock):
    eng._on_minute(row(50, -20))
    selected = eng._scan(clock["now"])
    assert selected is not None
    buy, sell, _ = selected
    reserved = eng._prepare_range_submit(buy, sell)
    assert reserved is not None
    return reserved, buy, sell


def legacy_range_submission_valid(eng, reserved, buy, sell, buy_bound, sell_bound):
    """Pre-instrumentation decision as a parity oracle for fixed fixtures."""
    eng._load_persisted_halt()
    now = time.time()
    if not eng._range_venues_ready(now, check_locks=False, check_rate=False):
        return False
    try:
        current, reason = eng._range_plan_now(now, reserved=True)
    except RangeStateError as exc:
        eng._halt_rolling(f"range pre-submit state mismatch: {exc}")
        return False
    intent = eng._range.state["pending_intent"]
    if (current is None or current.signal != reserved.signal or intent is None
            or intent["minute_ts"] != reserved.signal.minute_ts
            or intent["qty"] != reserved.plan.qty
            or current.direction != reserved.direction
            or current.plan.reduce_only != reserved.plan.reduce_only
            or current.plan.qty + 1e-9 < reserved.plan.qty):
        return False
    slip = eng.cfg.leg_slippage_bps / 1e4
    fresh_buy_bound = buy.px_round(current.plan.buy_limit * (1 + slip), round_up=False)
    fresh_sell_bound = sell.px_round(current.plan.sell_limit * (1 - slip), round_up=True)
    if not (buy.book.best_ask() <= buy_bound <= fresh_buy_bound + 1e-9
            and fresh_sell_bound - 1e-9 <= sell_bound <= sell.book.best_bid()):
        return False
    return all(reserved.plan.qty + 1e-9 >= venue.min_base and bound > 0
               and reserved.plan.qty * bound + 1e-9
               >= max(eng._min_notional, venue.min_quote)
               for venue, bound in ((buy, buy_bound), (sell, sell_bound)))


def _patch_current_range_plan(monkeypatch, eng, reserved, *, qty=None,
                              buy_limit=None, sell_limit=None):
    current_plan = SimpleNamespace(
        qty=reserved.plan.qty if qty is None else qty,
        reduce_only=reserved.plan.reduce_only,
        buy_limit=reserved.plan.buy_limit if buy_limit is None else buy_limit,
        sell_limit=reserved.plan.sell_limit if sell_limit is None else sell_limit,
    )
    current = SimpleNamespace(
        signal=reserved.signal,
        direction=reserved.direction,
        plan=current_plan,
    )
    monkeypatch.setattr(
        eng, "_range_plan_now",
        lambda _now, reserved=False: (current, "ok"),
    )
    return current


def test_persisted_event_000008_favorable_sell_revalidation_passes(
        tmp_path, monkeypatch):
    """Event 1791444610170-000008 must pass after the monotonicity fix."""
    eng, _, clock = make_range(tmp_path, monkeypatch)
    reserved, buy, sell = prepared_range(eng, clock)

    # Persisted diagnostics for 1791444610170-000008:
    # reserved sell bound=2149.3, fresh sell bound=2149.4,
    # RH best bid=2151.5; the old oracle rejected this as sell_price_bound.
    buy_bound, sell_bound = 2112.8, 2149.3
    buy.set_book(2110.6, 2110.7, sz=100)
    sell.set_book(2151.5, 2151.6, sz=100)
    _patch_current_range_plan(
        monkeypatch,
        eng,
        reserved,
        buy_limit=buy_bound / (1 + eng.cfg.leg_slippage_bps / 1e4),
        sell_limit=2149.4 / (1 - eng.cfg.leg_slippage_bps / 1e4),
    )

    old = legacy_range_submission_valid(
        eng, reserved, buy, sell, buy_bound, sell_bound)
    verdict = eng._range_submission_verdict(
        reserved, buy, sell, buy_bound, sell_bound)

    assert old is False
    assert verdict["ok"] is True
    assert verdict["reason"] == "ok"
    assert verdict["details"]["fresh_sell_bound"] == pytest.approx(2149.4)
    assert verdict["details"]["reserved_sell_bound"] == pytest.approx(2149.3)


@pytest.mark.parametrize(("case", "buy_fresh", "sell_fresh", "buy_ask",
                          "sell_bid", "expected"), [
    ("buy_favorable", "below", "same", "reserved", "reserved", True),
    ("sell_adverse_inside", "same", "below", "reserved", "reserved", True),
    ("buy_adverse_inside", "above", "same", "reserved", "reserved", True),
    ("sell_below_reserved", "same", "same", "reserved", "below", False),
    ("buy_above_reserved", "same", "same", "above", "reserved", False),
])
def test_price_bound_revalidation_uses_reserved_bounds_only(
        tmp_path, monkeypatch, case, buy_fresh, sell_fresh, buy_ask,
        sell_bid, expected):
    eng, _, clock = make_range(tmp_path, monkeypatch)
    reserved, buy, sell = prepared_range(eng, clock)
    buy_bound, sell_bound = reserved.plan.buy_limit, reserved.plan.sell_limit
    slip = eng.cfg.leg_slippage_bps / 1e4

    buy_current_fresh = {
        "below": buy_bound - 0.1,
        "same": buy_bound,
        "above": buy_bound + 0.1,
    }[buy_fresh]
    sell_current_fresh = {
        "below": sell_bound - 0.1,
        "same": sell_bound,
        "above": sell_bound + 0.1,
    }[sell_fresh]
    _patch_current_range_plan(
        monkeypatch,
        eng,
        reserved,
        buy_limit=buy_current_fresh / (1 + slip),
        sell_limit=sell_current_fresh / (1 - slip),
    )

    current_ask = {
        "reserved": buy_bound,
        "above": buy_bound + 0.1,
    }[buy_ask]
    current_bid = {
        "reserved": sell_bound,
        "below": sell_bound - 0.1,
    }[sell_bid]
    buy.set_book(current_ask - 0.1, current_ask, sz=100)
    sell.set_book(current_bid, current_bid + 0.1, sz=100)

    verdict = eng._range_submission_verdict(
        reserved, buy, sell, buy_bound, sell_bound)

    assert verdict["ok"] is expected, case
    if not expected:
        assert verdict["reason"] in {"buy_price_bound", "sell_price_bound"}


@pytest.mark.parametrize("case", ["valid", "stale", "missing_intent", "buy_bound"])
def test_submission_verdict_boolean_matches_preinstrumentation_guard(
        tmp_path, monkeypatch, case):
    eng, _, clock = make_range(tmp_path, monkeypatch)
    reserved, buy, sell = prepared_range(eng, clock)
    buy_bound, sell_bound = reserved.plan.buy_limit, reserved.plan.sell_limit
    if case == "stale":
        clock["now"] = 3076.0
    elif case == "missing_intent":
        eng._range.state["pending_intent"] = None
    elif case == "buy_bound":
        buy_bound = 0.01

    before = legacy_range_submission_valid(
        eng, reserved, buy, sell, buy_bound, sell_bound)
    after = eng._range_submission_verdict(
        reserved, buy, sell, buy_bound, sell_bound)["ok"]
    assert type(after) is bool
    assert after is before


@pytest.mark.parametrize(("plan_reason", "expected"), [
    ("stale_signal", "stale_signal"),
    ("coverage", "coverage"),
    ("range_gate", "range_gate"),
    ("inflight", "inflight"),
    ("minute_consumed", "minute_consumed"),
    ("stale_book", "stale_or_pretrade_book"),
    ("empty_book", "stale_or_pretrade_book"),
    ("below_min_base", "below_min_base"),
    ("below_min_notional", "below_min_notional"),
    ("at_target", "no_current_plan"),
])
def test_submission_verdict_maps_current_plan_block_reason(
        tmp_path, monkeypatch, plan_reason, expected):
    eng, _, clock = make_range(tmp_path, monkeypatch)
    reserved, buy, sell = prepared_range(eng, clock)
    monkeypatch.setattr(eng, "_range_plan_now",
        lambda _now, reserved=False: (None, plan_reason))

    verdict = eng._range_submission_verdict(
        reserved, buy, sell, reserved.plan.buy_limit, reserved.plan.sell_limit)

    assert verdict["ok"] is False
    assert verdict["reason"] == expected


@pytest.mark.parametrize(("blocked_by", "expected"), [
    ("halted", "halted"),
    ("stop", "stop_requested"),
    ("venue_down", "venue_down"),
    ("not_ready", "venue_not_ready"),
    ("limited", "venue_limited"),
    ("pretrade_book", "stale_or_pretrade_book"),
])
def test_submission_verdict_names_venue_readiness_block(
        tmp_path, monkeypatch, blocked_by, expected):
    eng, _, clock = make_range(tmp_path, monkeypatch)
    reserved, buy, sell = prepared_range(eng, clock)
    if blocked_by == "halted":
        eng.halted = True
    elif blocked_by == "stop":
        eng.stop.set()
    elif blocked_by == "venue_down":
        eng._venue_down[buy.key] = clock["now"]
    elif blocked_by == "not_ready":
        monkeypatch.setattr(buy, "ready_to_trade", lambda: False)
    elif blocked_by == "limited":
        monkeypatch.setattr(eng, "_venue_limited", lambda _venue: True)
    else:
        buy.last_traded_ts = buy.book.last_update_ts

    verdict = eng._range_submission_verdict(
        reserved, buy, sell, reserved.plan.buy_limit, reserved.plan.sell_limit)

    assert verdict["ok"] is False
    assert verdict["reason"] == expected


@pytest.mark.parametrize(("change", "expected"), [
    ("minute", "minute_mismatch"),
    ("qty", "qty_mismatch"),
    ("signal", "signal_changed"),
    ("direction", "direction_changed"),
    ("reduce_only", "reduce_only_changed"),
    ("qty_shrinks", "current_qty_below_reserved"),
    ("sell_bound", "sell_price_bound"),
    ("below_min_base", "below_min_base"),
    ("below_min_notional", "below_min_notional"),
])
def test_submission_verdict_identifies_intent_plan_and_bound_blocks(
        tmp_path, monkeypatch, change, expected):
    eng, _, clock = make_range(tmp_path, monkeypatch)
    reserved, buy, sell = prepared_range(eng, clock)
    intent = eng._range.state["pending_intent"]
    current_signal = reserved.signal
    direction = reserved.direction
    current_qty = reserved.plan.qty
    reduce_only = reserved.plan.reduce_only
    if change == "minute":
        intent["minute_ts"] += 60
    elif change == "qty":
        intent["qty"] += 0.001
    elif change == "signal":
        current_signal = object()
    elif change == "direction":
        direction = "sell_entropy"
    elif change == "reduce_only":
        reduce_only = not reduce_only
    elif change == "qty_shrinks":
        current_qty = max(0.0, current_qty - 0.001)
    elif change == "below_min_base":
        buy.min_base = reserved.plan.qty + 1
    elif change == "below_min_notional":
        buy.min_quote = 1e9

    current_plan = SimpleNamespace(
        qty=current_qty, reduce_only=reduce_only,
        buy_limit=reserved.plan.buy_limit, sell_limit=reserved.plan.sell_limit)
    current = SimpleNamespace(signal=current_signal, direction=direction,
                              plan=current_plan)
    monkeypatch.setattr(eng, "_range_plan_now",
        lambda _now, reserved=False: (current, "ok"))
    buy_bound, sell_bound = reserved.plan.buy_limit, reserved.plan.sell_limit
    if change == "sell_bound":
        sell_bound = sell.book.best_bid() + 1

    verdict = eng._range_submission_verdict(
        reserved, buy, sell, buy_bound, sell_bound)

    assert verdict["ok"] is False
    assert verdict["reason"] == expected


def test_submission_verdict_reports_missing_pending_intent_separately(
        tmp_path, monkeypatch):
    eng, _, clock = make_range(tmp_path, monkeypatch)
    reserved, buy, sell = prepared_range(eng, clock)
    eng._range.state["pending_intent"] = None

    verdict = eng._range_submission_verdict(
        reserved, buy, sell, reserved.plan.buy_limit, reserved.plan.sell_limit)

    assert verdict["reason"] == "missing_pending_intent"
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


def test_submission_verdict_reports_specific_reason_and_valid_remains_boolean(
        tmp_path, monkeypatch):
    eng, _, clock = make_range(tmp_path, monkeypatch)
    eng._on_minute(row(50, -20))
    selected = eng._scan(clock["now"])
    assert selected is not None
    buy, sell, _ = selected
    reserved = eng._prepare_range_submit(buy, sell)
    assert reserved is not None

    clock["now"] = 3076.0
    verdict = eng._range_submission_verdict(reserved, buy, sell,
                                            reserved.plan.buy_limit,
                                            reserved.plan.sell_limit)

    assert verdict["ok"] is False
    assert verdict["reason"] == "stale_signal"
    assert isinstance(eng._range_submission_valid(
        reserved, buy, sell, reserved.plan.buy_limit,
        reserved.plan.sell_limit), bool)


def test_both_guard_stages_are_recorded_without_changing_submit_outcome(
        tmp_path, monkeypatch):
    eng, calls, _ = make_range(tmp_path, monkeypatch)
    eng._on_minute(row(50, -20))
    asyncio.run(eng._evaluate())

    assert len(calls) == 2
    with open(eng.cfg.trades_csv, newline="") as fh:
        row_out = next(csv.DictReader(fh))
    for leg in ("buy", "sell"):
        trace = json.loads(row_out[f"{leg}_guard_trace"])
        assert [item["stage"] for item in trace] == [
            "engine_preflight", "venue_pre_submit"]
        assert all(item["ok"] is True for item in trace)
        assert trace[0]["rate_budget_ok"] is True
        assert trace[1]["rate_budget_ok"] is None
        assert trace[1]["venue_guard_ts"] == "test-venue-guard"
        assert trace[1]["transport_attempted"] is True
        assert "ready" in trace[1]["venue_checks"]
        assert "limited" in trace[1]["venue_checks"]
        assert row_out[f"{leg}_transport_attempted"] == "1"
        assert float(row_out[f"{leg}_pre_submit_wait_ms"]) >= 0.0
    assert eng._range.signed_qty == pytest.approx(.528)
    assert not eng.halted


@pytest.mark.parametrize("block_reason", ["stale_signal", "venue_rate_budget"])
def test_engine_preflight_block_is_persisted_without_transport(
        tmp_path, monkeypatch, block_reason):
    eng, _, clock = make_range(tmp_path, monkeypatch)
    reserved, buy, sell = prepared_range(eng, clock)
    plan = reserved.plan
    monkeypatch.setattr(eng, "_prepare_range_submit",
                        lambda *_args, **_kwargs: reserved)
    original_verdict = eng._range_submission_verdict
    verdict_calls = {"count": 0}

    def selected_block(*args, **kwargs):
        verdict_calls["count"] += 1
        if verdict_calls["count"] == 1:
            return {"ok": False, "reason": "stale_signal",
                    "details": {"timestamp": "test", "venue_ready": {},
                                "venue_limited": {}}}
        return original_verdict(*args, **kwargs)

    if block_reason == "stale_signal":
        monkeypatch.setattr(eng, "_range_submission_verdict", selected_block)
    else:
        # The reservation is already safely prepared; the first rate check is
        # now blocked, matching the existing post-preflight budget boundary.
        buy.orders_per_min = 0

    execution = asyncio.run(eng._execute(buy, sell, plan))
    assert execution is not None
    info = execution["buy_info"]
    assert info["status"] == "pre-submit-blocked"
    assert info["reason"] == f"range_guard:{block_reason}"
    assert info["transport_attempted"] is False
    assert info["not_submitted"] is True
    assert info["guard_trace"][0]["stage"] == "engine_preflight"
    assert info["guard_trace"][0]["reason"] == block_reason
    if block_reason == "venue_rate_budget":
        assert info["guard_trace"][0]["rate_budget_ok"] is False


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
                "avg_px": None, "err": None, "unresolved": False,
                "reason": "range_guard:stale_or_pretrade_book",
                "transport_attempted": False,
                "venue_guard_ts": "test-venue-guard", "not_submitted": True}
    eng.entropy.send_taker, eng.hedge.send_taker = entropy_send, rh_send
    asyncio.run(eng._evaluate())
    assert signed == ["entropy-primary", "entropy-residual"]
    assert eng._range.signed_qty == 0
    assert eng._range.state["pending_intent"] is None
    assert not eng.halted
    with open(eng.cfg.trades_csv, newline="") as fh:
        row_out = next(csv.DictReader(fh))
    assert row_out["sell_status"] == "pre-submit-blocked"
    assert row_out["sell_reason"] == "range_guard:stale_or_pretrade_book"
    sell_trace = json.loads(row_out["sell_guard_trace"])
    assert [item["stage"] for item in sell_trace] == [
        "engine_preflight", "venue_pre_submit"]
    assert sell_trace[-1]["ok"] is False
    assert sell_trace[-1]["reason"] == "stale_or_pretrade_book"
    assert sell_trace[-1]["venue_guard_ts"] == "test-venue-guard"
    assert sell_trace[-1]["transport_attempted"] is False
    assert row_out["sell_transport_attempted"] == "0"


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
    kwargs = {"submit_guard": (lambda: allow)}
    result = asyncio.run(venue.send_taker(is_buy=True, qty=.2, limit_px=100, **kwargs))
    if allow:
        assert len(posted) == 1
        assert result["filled_base"] == .2
        assert result["transport_attempted"] is True
        assert isinstance(result["signing_ms"], float)
        assert result["signing_ms"] >= 0.0
        assert result["venue_guard_ts"]
    else:
        assert posted == []
        assert result["not_submitted"] is True
        assert result["transport_attempted"] is False
        assert isinstance(result["signing_ms"], float)
        assert result["venue_guard_ts"]
