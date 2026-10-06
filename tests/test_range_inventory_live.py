"""No network: real planner/state, fake market constraints only."""
import importlib.util
import json
from dataclasses import replace

import pytest

from test_engine import StubVenue
from test_range_inventory_shadow import row, small_params


def live_type():
    assert importlib.util.find_spec("entropy_arb.range_inventory_live"), "live adapter missing"
    from entropy_arb.range_inventory_live import RangeInventoryLive
    return RangeInventoryLive


def adapter(tmp_path, *, params=None, positions=None):
    cls = live_type()
    from entropy_arb.range_inventory_live import CANARY_PARAMS
    live = cls(str(tmp_path / "range-ANTH-lighter-rh.json"), symbol="ANTH",
               hedge="lighter-rh", params=params or replace(CANARY_PARAMS,
                   long_window_minutes=4, short_window_minutes=50,
                   range_gate_window_minutes=4, min_coverage_pct=100))
    live.load_and_reconcile(positions or {"entropy": 0, "hedge": 0})
    for i in range(50):
        live.core.warmup_row(row(i, float(i)))
    return live


def venues(qty=100):
    e, h = StubVenue("entropy", "ENTROPY", cap=1500), StubVenue("hedge", "RH", cap=1500)
    e.set_book(99.9, 100.1, sz=qty)
    h.set_book(99.9, 100.1, sz=qty)
    return e, h


def plan(live, now=3061, *, qty=100, min_base=.001, min_notional=10, max_order=90):
    e, h = venues(qty)
    e.position, h.position = live.signed_qty, -live.signed_qty
    e.book.last_update_ts = h.book.last_update_ts = now
    return live.plan(now=now, entropy=e, hedge=h, step=.001,
                     min_base=min_base, min_notional=min_notional,
                     max_order_notional=max_order, staleness_sec=3)


def fill(venue, side, qty, px=100):
    return {"venue": venue, "side": side, "qty": qty, "avg_px": px, "fee_bps": 0}


def build(live, qty=.3):
    assert live.on_minute(row(50, -20), now=3061)
    result, reason = plan(live)
    assert reason == "ok"
    live.reserve(result, now=3061)
    live.settle({"entropy": qty, "hedge": -qty},
                [fill("entropy", "buy", qty), fill("hedge", "sell", qty)])


def test_completed_minute_immediately_plans_from_live_bbo(tmp_path):
    live = adapter(tmp_path)
    assert live.on_minute(row(50, -20, bid=200, ask=200), now=3061)
    result, reason = plan(live)
    assert reason == "ok"
    assert result.plan.buy_limit == 100.1  # live, NOT recorder's 200
    assert result.plan.qty == .529
    assert result.plan.buy_notional <= 53
    assert result.action == "long_build"


@pytest.mark.parametrize("now,accepted,reason", [
    (3059, False, "no_signal"), (3061, True, "ok"),
    (3075, True, "ok"), (3075.001, True, "stale_signal"),
])
def test_signal_age_and_partial_minute_fail_closed(tmp_path, now, accepted, reason):
    live = adapter(tmp_path)
    assert live.on_minute(row(50, -20), now=now) is accepted
    _, actual = plan(live, now=now)
    assert actual == reason


def test_depth_fraction_and_no_hidden_second_level(tmp_path):
    live = adapter(tmp_path)
    live.on_minute(row(50, -20), now=3061)
    result, _ = plan(live, qty=.2)
    assert result.plan.qty == .15
    assert result.depth_used_fraction == pytest.approx(.75)


def test_gate_only_blocks_add_not_reduce(tmp_path):
    live = adapter(tmp_path, params=small_params(short_window_minutes=50,
        long_cap_usd=1500, short_cap_usd=1500, hard_cap_usd=1500,
        max_adjust_usd=53, range_gate_min_bps=1000))
    live.on_minute(row(50, -20), now=3061)
    assert plan(live)[1] == "range_gate"
    # A reconciled prior position has its own state, never imported rolling lots.
    live2 = adapter(tmp_path / "second")
    build(live2)
    for i in range(51, 55):
        live2.on_minute(row(i, .01 * i), now=i * 60 + 61)
    assert live2.signal.range_gate_open is False
    result, reason = plan(live2, now=3301)
    assert reason == "ok"
    assert result.plan.reduce_only
    assert result.action == "long_release"


def test_reserve_persists_single_attempt_budget_before_fill(tmp_path):
    live = adapter(tmp_path)
    live.on_minute(row(50, -20), now=3061)
    result, _ = plan(live)
    live.reserve(result, now=3061)
    saved = json.loads((tmp_path / "range-ANTH-lighter-rh.json").read_text())
    assert saved["consumed_minute_ts"] == 3000
    assert saved["pending_intent"]["qty"] == .529
    assert plan(live)[1] == "inflight"
    with pytest.raises(RuntimeError, match="in-flight"):
        live_type()(str(tmp_path / "range-ANTH-lighter-rh.json"), symbol="ANTH",
                    hedge="lighter-rh", params=live.params).load_and_reconcile({"entropy": 0, "hedge": 0})
    live.settle({"entropy": .1, "hedge": -.1},
                [fill("entropy", "buy", .1), fill("hedge", "sell", .1)])
    assert plan(live)[1] == "minute_consumed"
    assert not live.on_minute(row(50, -30), now=3062)


def test_partial_and_residual_close_uses_actual_quantities(tmp_path):
    live = adapter(tmp_path)
    build(live)
    live.on_minute(row(51, 100), now=3121)
    result, _ = plan(live, now=3121)
    assert result.plan.reduce_only
    live.reserve(result, now=3121)
    live.settle({"entropy": .2, "hedge": -.2}, [
        fill("entropy", "sell", .1, 101), fill("hedge", "buy", .05, 100),
        fill("hedge", "buy", .05, 100),
    ])
    assert live.signed_qty == pytest.approx(.2)
    assert live.state["current_direction"] == "buy_entropy"
    assert live.state["cumulative_realized_capture_usd"] == pytest.approx(.1)
    assert live.signal.target_usd == 0
    assert plan(live, now=3121)[1] == "minute_consumed"


def test_missing_actual_price_closes_qty_but_telemetry_unavailable(tmp_path):
    live = adapter(tmp_path)
    build(live)
    live.on_minute(row(51, 100), now=3121)
    result, _ = plan(live, now=3121)
    live.reserve(result, now=3121)
    live.settle({"entropy": 0, "hedge": 0}, [
        fill("entropy", "sell", .3, None), fill("hedge", "buy", .3, 100),
    ])
    assert live.signed_qty == 0
    assert live.state["current_direction"] is None
    assert live.state["cumulative_realized_capture_usd"] is None


def test_restart_reconciles_exact_state_and_rejects_mismatch(tmp_path):
    live = adapter(tmp_path)
    build(live)
    restarted = live_type()(live.path, symbol="ANTH", hedge="lighter-rh", params=live.params)
    restarted.load_and_reconcile({"entropy": .3, "hedge": -.3})
    assert restarted.expected_positions() == {"entropy": .3, "hedge": -.3}
    with pytest.raises(RuntimeError, match="mismatch"):
        restarted.load_and_reconcile({"entropy": .31, "hedge": -.3})


@pytest.mark.parametrize("content", ["{broken", '{"schema_version":1}',
                                         '{"signed_qty":0.1}'])
def test_corrupt_state_is_never_reset(tmp_path, content):
    path = tmp_path / "range.json"
    path.write_text(content)
    live = live_type()(str(path), symbol="ANTH", hedge="lighter-rh")
    with pytest.raises(RuntimeError):
        live.load_and_reconcile({"entropy": 0, "hedge": 0})
    assert path.read_text() == content


def test_no_state_cannot_adopt_nonflat_positions(tmp_path):
    live = live_type()(str(tmp_path / "absent.json"), symbol="ANTH", hedge="lighter-rh")
    with pytest.raises(RuntimeError, match="mismatch"):
        live.load_and_reconcile({"entropy": .015, "hedge": -.015})


def test_venue_minima_and_headroom_remain_enforced(tmp_path):
    live = adapter(tmp_path)
    live.on_minute(row(50, -20), now=3061)
    assert plan(live, min_base=.6)[1] == "below_min_base"
    assert plan(live, min_notional=54)[1] == "below_min_notional"
    e, h = venues()
    e.cap_usd = h.cap_usd = 5
    e.book.last_update_ts = h.book.last_update_ts = 3061
    assert live.plan(now=3061, entropy=e, hedge=h, step=.001, min_base=.001,
                     min_notional=10, max_order_notional=90, staleness_sec=3)[0] is None


def test_write_failure_prevents_reservation(tmp_path, monkeypatch):
    live = adapter(tmp_path)
    live.on_minute(row(50, -20), now=3061)
    result, _ = plan(live)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr("entropy_arb.range_inventory_live.os.replace", fail)
    with pytest.raises(RuntimeError, match="persist"):
        live.reserve(result, now=3061)
    assert live.state["pending_intent"] is None


def test_reverse_requires_actual_flat_and_next_completed_signal(tmp_path):
    live = adapter(tmp_path)
    build(live)
    live.on_minute(row(51, 100), now=3121)
    result, _ = plan(live, now=3121)
    assert result.direction == "sell_entropy"
    assert result.plan.reduce_only
    assert result.plan.qty == .3
    live.reserve(result, now=3121)
    live.settle({"entropy": 0, "hedge": 0}, [
        fill("entropy", "sell", .3), fill("hedge", "buy", .3),
    ])
    assert plan(live, now=3122)[0] is None
    live.on_minute(row(52, 110), now=3181)
    reverse, reason = plan(live, now=3181)
    assert reason == "ok"
    assert reverse.direction == "sell_entropy"
    assert not reverse.plan.reduce_only


def test_state_signal_metadata_corruption_rejected(tmp_path):
    live = adapter(tmp_path)
    live.on_minute(row(50, -20), now=3061)
    saved = json.loads(open(live.path).read())
    saved["signal_metadata"]["minute_ts"] = 1
    with open(live.path, "w") as fh:
        json.dump(saved, fh)
    restarted = live_type()(live.path, symbol="ANTH", hedge="lighter-rh", params=live.params)
    with pytest.raises(RuntimeError, match="signal"):
        restarted.load_and_reconcile({"entropy": 0, "hedge": 0})


def test_unknown_residual_retains_intent_not_false_flat(tmp_path):
    live = adapter(tmp_path)
    build(live)
    live.on_minute(row(51, 100), now=3121)
    result, _ = plan(live, now=3121)
    live.reserve(result, now=3121)
    with pytest.raises(RuntimeError, match="unresolved"):
        live.settle({"entropy": 0, "hedge": -.1}, [
            fill("entropy", "sell", .3), fill("hedge", "buy", .2),
        ])
    assert live.signed_qty == .3
    assert live.state["pending_intent"] is not None


def test_nonfinite_live_depth_on_either_leg_is_blocked(tmp_path):
    live = adapter(tmp_path)
    live.on_minute(row(50, -20), now=3061)
    e, h = venues()
    e.book.last_update_ts = h.book.last_update_ts = 3061
    h.book.bids = {99.9: float("nan")}
    result, reason = live.plan(now=3061, entropy=e, hedge=h, step=.001,
        min_base=.001, min_notional=10, max_order_notional=90, staleness_sec=3)
    assert result is None
    assert reason == "empty_depth"


def test_budget_accounts_for_execution_price_bounds(tmp_path):
    live = adapter(tmp_path)
    live.on_minute(row(50, -20), now=3061)
    e, h = venues()
    e.book.last_update_ts = h.book.last_update_ts = 3061
    result, _ = live.plan(now=3061, entropy=e, hedge=h, step=.0001,
        min_base=.0001, min_notional=10, max_order_notional=90,
        staleness_sec=3, leg_slippage_bps=10)
    assert result.plan.qty * result.plan.buy_limit * 1.001 <= 53


@pytest.mark.parametrize("primary_px,hedge_px", [(None, None), (None, 100), (100, None)])
def test_unknown_price_roundtrip_persists_unavailable_not_zero(tmp_path, primary_px, hedge_px):
    live = adapter(tmp_path)
    live.on_minute(row(50, -20), now=3061)
    result, _ = plan(live)
    live.reserve(result, now=3061)
    settled = live.settle({"entropy": 0, "hedge": 0}, [
        fill("entropy", "buy", .2, primary_px),
        fill("entropy", "sell", .2, hedge_px),
    ])
    assert settled["paired_fill_qty"] == 0
    assert settled["realized_capture_usd"] is None
    assert live.state["cumulative_realized_capture_usd"] is None
    assert live.state["pending_intent"] is None
    restarted = live_type()(live.path, symbol="ANTH", hedge="lighter-rh", params=live.params)
    restarted.load_and_reconcile({"entropy": 0, "hedge": 0})
    assert restarted.state["cumulative_realized_capture_usd"] is None


@pytest.mark.parametrize("inventory", [.1, -.1])
def test_minimum_notional_checked_at_rounded_protective_prices(tmp_path, inventory):
    live = adapter(tmp_path)
    build(live, qty=abs(inventory))
    if inventory < 0:
        live._commit(dict(live.state, entropy_qty=inventory, hedge_qty=-inventory,
                          current_direction="sell_entropy"))
    live.on_minute(row(51, 100 if inventory > 0 else -100), now=3121)
    e, h = venues()
    for venue in (e, h):
        venue.set_book(100, 100)
        venue.book.last_update_ts = 3121
    e.position, h.position = inventory, -inventory
    result, reason = live.plan(now=3121, entropy=e, hedge=h, step=.001,
        min_base=.001, min_notional=10, max_order_notional=90,
        staleness_sec=3, leg_slippage_bps=10)
    assert result is None  # 0.1 * sell protection 99.9 = 9.99, not legal $10
    assert reason == "below_min_notional"


@pytest.mark.parametrize("inventory", [5.0, -5.0])
def test_gate_blocked_metadata_cannot_block_price_drift_reduction(tmp_path, inventory):
    from entropy_arb.range_inventory_live import CANARY_PARAMS
    live = adapter(tmp_path, params=replace(CANARY_PARAMS, long_window_minutes=4,
        short_window_minutes=50, range_gate_window_minutes=4, min_coverage_pct=100,
        range_gate_min_bps=1000))
    live._commit(dict(live.state, entropy_qty=inventory, hedge_qty=-inventory,
        current_direction="buy_entropy" if inventory > 0 else "sell_entropy",
        mean_cost_per_base=0))
    live.on_minute(row(50, -20 if inventory > 0 else 100), now=3061)
    assert live.signal.exposure_blocked
    assert not live.signal.range_gate_open
    e, h = venues()
    for venue in (e, h):
        venue.set_book(110, 110, sz=100)
        venue.book.last_update_ts = 3061
    e.position, h.position = inventory, -inventory
    result, reason = live.plan(now=3061, entropy=e, hedge=h, step=.001,
        min_base=.001, min_notional=10, max_order_notional=90, staleness_sec=3)
    assert reason == "ok"
    assert result.plan.reduce_only
    assert result.plan.qty < abs(inventory)
