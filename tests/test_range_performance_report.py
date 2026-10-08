from __future__ import annotations

import subprocess
import sys
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from tools import range_performance_report as report

SCRIPT = REPO_ROOT / "tools" / "range_performance_report.py"


def test_cli_help_exposes_read_only_modes_and_recorder_override():
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--offline" in result.stdout
    assert "--recorder" in result.stdout


def _inputs(tmp_path, *, offline=True, recorder="minutes-real.csv", state=None):
    return report.resolve_inputs(
        root=tmp_path,
        symbol="ANTH",
        hedge="lighter-rh",
        recorder_override=Path(recorder) if recorder else None,
        offline=offline,
        state_path=Path(state) if state else Path("logs/state/range-ANTH-lighter-rh.json"),
        halt_path=Path("logs/state/halt-ANTH-lighter-rh.json"),
        trades_path=Path("logs/trades/trades-ANTH-lighter-rh.csv"),
    )


def test_explicit_recorder_precedes_runtime_config_and_runtime_paths_are_namespaced(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "recorder:\n  csv: custom/minutes.csv\nlogging:\n  trades_csv: custom/trades.csv\n"
    )
    inputs = report.resolve_inputs(
        root=tmp_path, symbol="ANTH", hedge="lighter-rh",
        recorder_override=Path("override/my.csv"), config_path=Path("config.yaml"),
        state_path=Path("state/range.json"), halt_path=Path("state/halt.json"),
    )
    assert inputs.paths.recorder == tmp_path / "override/my.csv"
    assert inputs.paths.trades == tmp_path / "custom/trades-ANTH-lighter-rh.csv"

    resolved = report.resolve_inputs(root=tmp_path, symbol="ANTH", hedge="lighter-rh",
                                     config_path=Path("config.yaml"))
    assert resolved.paths.recorder == tmp_path / "custom/minutes-ANTH-lighter-rh.csv"


def test_path_resolution_fails_closed_when_runtime_recorder_is_not_unique(tmp_path):
    (tmp_path / "config.yaml").write_text("logging:\n  trades_csv: logs/trades.csv\n")
    with pytest.raises(report.ReportError, match="recorder path cannot be uniquely resolved"):
        report.resolve_inputs(root=tmp_path, symbol="ANTH", hedge="lighter-rh",
                              config_path=Path("config.yaml"))


def test_offline_requires_explicit_recorder_and_never_reads_config_or_env(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("invalid: [")
    (tmp_path / ".env").write_text("HL_PRIVATE_KEY=must-not-be-read\n")
    with pytest.raises(report.ReportError, match="--offline requires --recorder"):
        report.resolve_inputs(root=tmp_path, symbol="ANTH", hedge="lighter-rh", offline=True)
    inputs = report.resolve_inputs(root=tmp_path, symbol="ANTH", hedge="lighter-rh",
                                   offline=True, recorder_override=Path("minutes.csv"))
    assert inputs.hl_account_address is None
    assert inputs.paths.config is None
    assert inputs.paths.recorder == tmp_path / "minutes.csv"


def _write_snapshot_files(tmp_path, state):
    inputs = _inputs(tmp_path)
    inputs.paths.state.parent.mkdir(parents=True, exist_ok=True)
    inputs.paths.halt.parent.mkdir(parents=True, exist_ok=True)
    inputs.paths.trades.parent.mkdir(parents=True, exist_ok=True)
    inputs.paths.state.write_text(__import__("json").dumps(state))
    inputs.paths.halt.write_text('{"halted":true,"timestamp":"halt-1"}')
    inputs.paths.trades.write_text("event_id,ts\n")
    inputs.paths.recorder.parent.mkdir(parents=True, exist_ok=True)
    inputs.paths.recorder.write_text("symbol,hedge,minute_ts\n")
    return inputs


def _flat_state(**extra):
    return {"entropy_qty": 0, "hedge_qty": 0, "pending_intent": None,
            "consumed_minute_ts": None, "current_target_usd": 0,
            "signal_metadata": None, **extra}


def test_consistent_snapshot_accepts_stable_state_and_source_boundaries(tmp_path):
    inputs = _write_snapshot_files(tmp_path, _flat_state())
    mark = report.MarketMark(False, reason="offline")
    snapshot = report.capture_consistent_snapshot(inputs, lambda _: mark)
    assert snapshot.consistent
    assert snapshot.attempts == 1
    assert snapshot.state["entropy_qty"] == 0
    assert report.snapshot_still_current(inputs, snapshot)


def test_post_enrichment_revalidation_detects_state_and_source_changes(tmp_path):
    inputs = _write_snapshot_files(tmp_path, _flat_state())
    snapshot = report.capture_consistent_snapshot(
        inputs, lambda _: report.MarketMark(False, reason="offline"))
    assert report.snapshot_still_current(inputs, snapshot)

    inputs.paths.state.write_text(__import__("json").dumps(
        _flat_state(consumed_minute_ts="next-minute")))
    assert not report.snapshot_still_current(inputs, snapshot)

    inputs.paths.state.write_text(__import__("json").dumps(_flat_state()))
    with inputs.paths.trades.open("a") as stream:
        stream.write("new-event,1\n")
    assert not report.snapshot_still_current(inputs, snapshot)


@pytest.mark.parametrize("mutation", ["state", "append", "replace"])
def test_consistent_snapshot_retries_and_fails_closed_when_sources_move(tmp_path, mutation):
    inputs = _write_snapshot_files(tmp_path, _flat_state())
    calls = {"n": 0}

    def quote(_state):
        calls["n"] += 1
        if mutation == "state":
            payload = _flat_state(entropy_qty=0.25 if calls["n"] % 2 else 0.5,
                                  hedge_qty=-0.25 if calls["n"] % 2 else -0.5)
            inputs.paths.state.write_text(__import__("json").dumps(payload))
        elif mutation == "append":
            with inputs.paths.trades.open("a") as stream:
                stream.write(f"evt{calls['n']},1\n")
        else:
            old = inputs.paths.recorder
            replacement = old.with_suffix(f".{calls['n']}.replacement")
            replacement.write_text(old.read_text())
            replacement.replace(old)
        return report.MarketMark(False, reason="fixture")

    snapshot = report.capture_consistent_snapshot(inputs, quote, retries=2)
    assert not snapshot.consistent
    assert snapshot.attempts == 2
    assert "inconsistent_snapshot" in snapshot.reason


def _event(event_id, ts, direction, reduce_only, fills, matched):
    return report.PairEvent(event_id, ts, ts - 1, direction, reduce_only,
                            matched, fills)


def _fill(fid, event, ts, venue, side, qty, price, fee=None, fallback=False):
    return report.CanonicalFill(fid, event, ts, venue, side, qty, price,
                                fallback, fee, "complete" if fee is not None else "unknown")


def test_weighted_average_cost_long_release_and_executable_liquidation():
    build = _event("b1", 100, "buy_entropy", False, [
        _fill("b1:buy", "b1", 100, "entropy", "buy", 2, 100, 0.1),
        _fill("b1:sell", "b1", 100, "hedge", "sell", 2, 101, 0.1)], 2)
    reduce = _event("r1", 200, "buy_entropy", True, [
        _fill("r1:sell", "r1", 200, "entropy", "sell", 1, 102, 0.1),
        _fill("r1:buy", "r1", 200, "hedge", "buy", 1, 100, 0.1)], 1)
    state = {"entropy_qty": 1, "hedge_qty": -1, "mean_cost_per_base": -0.4,
             "pending_intent": None}
    result = report.account_events([build, reduce], state,
        report.MarketMark(True, 101, 103, 100, 102))
    assert result.current_inventory == pytest.approx(1)
    assert result.gross_realized == pytest.approx(3)
    assert result.fee_net_realized == pytest.approx(2.7)
    assert result.fees_usd == pytest.approx(0.4)
    assert result.gross_unrealized == pytest.approx(0)
    assert result.fee_net_unrealized == pytest.approx(-0.1)
    assert not result.completed_cycles
    assert result.current_cycle["direction"] == "LONG"


def test_short_liquidation_uses_entropy_ask_and_hedge_bid():
    event = _event("s1", 100, "sell_entropy", False, [
        _fill("s1:sell", "s1", 100, "entropy", "sell", 1, 100, 0),
        _fill("s1:buy", "s1", 100, "hedge", "buy", 1, 99, 0)], 1)
    result = report.account_events([event], {"entropy_qty": -1, "hedge_qty": 1,
        "mean_cost_per_base": 1, "pending_intent": None},
        report.MarketMark(True, 98, 101, 97, 99))
    # Buy back Entropy at ask 101 and sell RH at bid 97 => -4 liquidation cash;
    # RangeInventoryLive stores net build cash as mean_cost (-1 here).
    assert result.gross_unrealized == pytest.approx(-3)


def test_fallback_cashflow_is_realized_once_and_is_diagnostic_only():
    event = _event("f1", 100, "buy_entropy", False, [
        _fill("f1:fallback", "f1", 100, "hedge", "sell", 0.1, 100, 0, True)], 0)
    result = report.account_events([event], {"entropy_qty": 0, "hedge_qty": 0,
        "pending_intent": None}, report.MarketMark(False))
    assert result.gross_realized == pytest.approx(10)
    assert result.fallback_count == 1
    assert result.current_inventory == 0


def _trade_row(event_id, *, ts="100", direction="buy_entropy", reduce_only="0",
               buy_venue="entropy", sell_venue="hedge", buy_fill="0.5",
               sell_fill="0.5", buy_avg_px="100", sell_avg_px="101",
               hedge_fill="0", hedge_avg_px="", hedge_venue="", hedge_side="",
               hedge_status="not_needed", unresolved="0", error="",
               qty="0.5", buy_limit="100", sell_limit="101",
               buy_bbo_px="100", buy_bbo_qty="2", sell_bbo_px="101",
               buy_reason="", sell_reason="", buy_status="", sell_status="",
               sell_bbo_qty="2", signal_ts="99"):
    return {
        "event_id": event_id, "ts": ts, "signal_ts": signal_ts,
        "direction": direction, "reduce_only": reduce_only,
        "qty": qty, "matched_qty": str(min(float(buy_fill), float(sell_fill))),
        "buy_venue": buy_venue, "sell_venue": sell_venue,
        "buy_fill": buy_fill, "sell_fill": sell_fill,
        "buy_avg_px": buy_avg_px, "sell_avg_px": sell_avg_px,
        "buy_limit": buy_limit, "sell_limit": sell_limit,
        "buy_bbo_px": buy_bbo_px, "buy_bbo_qty": buy_bbo_qty,
        "sell_bbo_px": sell_bbo_px, "sell_bbo_qty": sell_bbo_qty,
        "hedge_fill": hedge_fill, "hedge_avg_px": hedge_avg_px,
        "hedge_venue": hedge_venue, "hedge_side": hedge_side,
        "hedge_status": hedge_status, "unresolved": unresolved,
        "error": error, "buy_reason": buy_reason, "sell_reason": sell_reason,
        "buy_status": buy_status, "sell_status": sell_status,
    }


def _attribute_row(row):
    events, fills, anomalies = report.build_fill_ledger([row])
    if str(row.get("unresolved", "0")).lower() in {"1", "true"}:
        assert all("is unresolved" in anomaly for anomaly in anomalies)
    else:
        assert not anomalies
    return events, fills, report.build_execution_attributions(events)[0]


def test_fallback_attribution_no_fallback_is_neutral():
    events, fills, attribution = _attribute_row(_trade_row("plain"))
    assert not any(fill.fallback for fill in fills)
    assert attribution["fallback_used"] is False
    assert attribution["fallback_repair_cashflow_usd"] == 0
    assert attribution["fallback_counterfactual_impact_usd"] == 0
    assert attribution["fallback_impact_reason"] is None
    assert attribution["event_gross_pnl_impact_usd"] == pytest.approx(events[0].gross_cash)


def test_one_leg_fill_successful_fallback_uses_logged_intended_counterpart_price():
    row = _trade_row("build-fallback", buy_fill="0.5", sell_fill="0",
        sell_avg_px="", hedge_fill="0.5", hedge_avg_px="98",
        hedge_venue="hedge", hedge_side="sell", hedge_status="settled",
        sell_limit="101", qty="0.5", sell_reason="IOC partial fill")
    events, fills, item = _attribute_row(row)
    assert len(fills) == 2
    assert item["fallback_used"] is True
    assert item["net_qty_before_fallback"] == pytest.approx(0.5)
    assert item["net_qty_after_fallback"] == pytest.approx(0)
    assert item["normal_primary_cashflow_usd"] == pytest.approx(-50)
    assert item["fallback_repair_cashflow_usd"] == pytest.approx(49)
    assert item["event_gross_pnl_impact_usd"] == pytest.approx(-1)
    assert item["fallback_counterfactual_impact_usd"] == pytest.approx(-1.5)
    assert item["fallback_counterfactual_price_source"] == "logged intended sell limit"
    assert item["fallback_reason"] == "sell leg: IOC partial fill"
    assert events[0].gross_cash == pytest.approx(item["event_gross_pnl_impact_usd"])


def test_release_side_fallback_attributes_missing_primary_buy_leg():
    row = _trade_row("release-fallback", direction="buy_entropy", reduce_only="1",
        buy_venue="hedge", sell_venue="entropy", buy_fill="0.4", sell_fill="0.6",
        buy_avg_px="99", sell_avg_px="100", hedge_fill="0.2", hedge_avg_px="100",
        hedge_venue="hedge", hedge_side="buy", hedge_status="settled",
        buy_limit="99", sell_limit="100", qty="0.6")
    _, _, item = _attribute_row(row)
    assert item["action"] == "release"
    assert item["net_qty_before_fallback"] == pytest.approx(-0.2)
    assert item["net_qty_after_fallback"] == pytest.approx(0)
    assert item["fallback_counterfactual_impact_usd"] == pytest.approx(-0.2)


def test_several_fallbacks_aggregate_impact_cashflow_and_event_rate():
    rows = [
        _trade_row("f1", buy_fill="0.5", sell_fill="0", sell_avg_px="",
            hedge_fill="0.5", hedge_avg_px="98", hedge_venue="hedge",
            hedge_side="sell", hedge_status="settled"),
        _trade_row("f2", ts="200", buy_fill="0.25", sell_fill="0",
            sell_avg_px="", hedge_fill="0.25", hedge_avg_px="97",
            hedge_venue="hedge", hedge_side="sell", hedge_status="settled",
            qty="0.25", sell_limit="100"),
        _trade_row("plain", ts="300"),
    ]
    events, _, _ = report.build_fill_ledger(rows)
    attributed = report.build_execution_attributions(events)
    fallbacks = [event for event in attributed if event["fallback_used"]]
    assert len(fallbacks) == 2
    assert sum(e["fallback_repair_cashflow_usd"] for e in attributed) == pytest.approx(73.25)
    assert sum(e["fallback_counterfactual_impact_usd"] for e in fallbacks) == pytest.approx(-2.25)
    assert sum(e["fallback_used"] for e in attributed) / len(attributed) == pytest.approx(2 / 3)


def test_missing_counterfactual_price_is_null_not_later_market_mark():
    row = _trade_row("unknown-price", buy_fill="0.5", sell_fill="0",
        sell_avg_px="", hedge_fill="0.5", hedge_avg_px="98",
        hedge_venue="hedge", hedge_side="sell", hedge_status="settled",
        sell_limit="", sell_bbo_px="")
    _, _, item = _attribute_row(row)
    assert item["fallback_counterfactual_impact_usd"] is None
    assert item["fallback_impact_reason"] == "insufficient_counterfactual_price"


def test_fallback_fill_is_counted_exactly_once_in_canonical_gross_cashflow():
    events, fills, item = _attribute_row(_trade_row("once", buy_fill="0.5",
        sell_fill="0", sell_avg_px="", hedge_fill="0.5", hedge_avg_px="98",
        hedge_venue="hedge", hedge_side="sell", hedge_status="settled"))
    assert [fill.fill_id for fill in fills] == ["once:buy", "once:fallback"]
    assert len({fill.fill_id for fill in fills}) == len(fills)
    assert sum(fill.cashflow for fill in fills) == pytest.approx(-1)
    assert item["fallback_repair_cashflow_usd"] == pytest.approx(49)
    assert events[0].gross_cash == pytest.approx(-1)


def test_unresolved_fallback_is_reported_without_fabricated_impact():
    row = _trade_row("unresolved", buy_fill="0.5", sell_fill="0",
        sell_avg_px="", hedge_fill="0", hedge_avg_px="", hedge_venue="hedge",
        hedge_side="sell", hedge_status="unresolved", unresolved="1",
        error="settlement timeout")
    events, fills, item = _attribute_row(row)
    assert item["fallback_used"] is True
    assert item["fallback_reason"] == "settlement timeout"
    assert item["fallback_qty"] == 0
    assert item["fallback_counterfactual_impact_usd"] is None
    assert item["fallback_impact_reason"] == "incomplete_fallback_repair"
    assert events[0].unresolved
    assert len(fills) == 1
    state = {"entropy_qty": 0.5, "hedge_qty": -0.5,
             "mean_cost_per_base": -1, "pending_intent": None}
    result = report.account_events(events, state, report.MarketMark(False))
    assert result.current_cycle["unresolved_count"] == 1
    assert result.completed_cycles == []


def test_fallback_attribution_does_not_change_canonical_gross_pnl():
    rows = [
        _trade_row("invariant-build", buy_fill="0.5", sell_fill="0",
            sell_avg_px="", hedge_fill="0.5", hedge_avg_px="98",
            hedge_venue="hedge", hedge_side="sell", hedge_status="settled"),
        _trade_row("invariant-release", ts="200", direction="buy_entropy",
            reduce_only="1", buy_venue="hedge", sell_venue="entropy",
            buy_fill="0.5", sell_fill="0.5", buy_avg_px="100", sell_avg_px="102",
            hedge_status="not_needed", qty="0.5"),
    ]
    events, _, anomalies = report.build_fill_ledger(rows)
    assert not anomalies
    state = {"entropy_qty": 0, "hedge_qty": 0,
             "mean_cost_per_base": None, "pending_intent": None}
    before = report.account_events(events, state, report.MarketMark(False))
    attributions = report.build_execution_attributions(events)
    after = report.account_events(events, state, report.MarketMark(False))
    assert attributions[0]["fallback_repair_cashflow_usd"] == pytest.approx(49)
    assert after.gross_realized == before.gross_realized
    assert after.gross_unrealized == before.gross_unrealized
    assert after.current_inventory == pytest.approx(0)
    assert after.completed_cycles[0]["gross_pnl_usd"] == pytest.approx(before.completed_cycles[0]["gross_pnl_usd"])
    assert after.completed_cycles[0]["fallback_counterfactual_impact_usd"] == pytest.approx(-1.5)


def test_completed_cycle_exports_fallback_attribution_and_uses_event_rate():
    rows = [
        _trade_row("cycle-build", buy_fill="0.5", sell_fill="0",
            sell_avg_px="", hedge_fill="0.5", hedge_avg_px="98",
            hedge_venue="hedge", hedge_side="sell", hedge_status="settled"),
        _trade_row("cycle-release", ts="200", reduce_only="1",
            buy_venue="hedge", sell_venue="entropy", buy_fill="0.5",
            sell_fill="0.5", buy_avg_px="100", sell_avg_px="102",
            hedge_status="not_needed"),
    ]
    events, _, anomalies = report.build_fill_ledger(rows)
    assert not anomalies
    result = report.account_events(events, {"entropy_qty": 0, "hedge_qty": 0,
        "mean_cost_per_base": None, "pending_intent": None}, report.MarketMark(False))
    cycle = result.completed_cycles[0]
    assert cycle["gross_pnl_usd"] == pytest.approx(0)
    assert cycle["normal_execution_contribution_usd"] == pytest.approx(-49)
    assert cycle["fallback_cashflow_usd"] == pytest.approx(49)
    assert cycle["fallback_counterfactual_impact_usd"] == pytest.approx(-1.5)
    assert cycle["fallback_count"] == 1
    assert cycle["fallback_rate"] == pytest.approx(0.5)
    assert cycle["gross_pnl_excluding_fallback_drag_usd"] == pytest.approx(1.5)
    assert cycle["fallback_drag_pct"] == pytest.approx(100)


def test_fallback_drag_is_na_when_attribution_incomplete_or_denominator_nonpositive():
    incomplete = report._cycle_output({"gross_realized": 2,
        "fallback_counterfactual_complete": False, "fallback_count": 1,
        "execution_event_count": 2, "unresolved_count": 0})
    assert incomplete["fallback_attribution_status"] == "incomplete"
    assert incomplete["fallback_counterfactual_impact_usd"] is None
    assert incomplete["fallback_drag_pct"] is None
    nonpositive = report._cycle_output({"gross_realized": -1,
        "fallback_counterfactual_complete": True,
        "fallback_counterfactual_impact": -1, "fallback_count": 1,
        "execution_event_count": 2, "unresolved_count": 0})
    assert nonpositive["gross_pnl_excluding_fallback_drag_usd"] == pytest.approx(0)
    assert nonpositive["fallback_drag_pct"] is None


def test_build_report_exports_event_attribution_json_and_html(tmp_path):
    row = _trade_row("html-fallback", buy_fill="0.5", sell_fill="0",
        sell_avg_px="", hedge_fill="0.5", hedge_avg_px="98",
        hedge_venue="hedge", hedge_side="sell", hedge_status="settled")
    row.update({
        "sell_guard_trace": json.dumps([
            {"stage": "engine_preflight", "ok": True, "reason": "ok"},
            {"stage": "venue_pre_submit", "ok": False,
             "reason": "stale_or_pretrade_book"},
        ], separators=(",", ":")),
        "sell_transport_attempted": "0",
        "buy_transport_attempted": "1",
        "sell_pre_submit_wait_ms": "7.5",
        "sell_nonce_wait_ms": "6.25",
    })
    events, _, anomalies = report.build_fill_ledger([row])
    assert not anomalies
    state = _flat_state(entropy_qty=0.5, hedge_qty=-0.5,
        mean_cost_per_base=-1, parameters={"hard_cap_usd": 1500})
    snapshot = report.Snapshot(state, {"halted": True}, [row], [], {}, consistent=True)
    metrics, html_text, _ = report.build_report(snapshot, _inputs(tmp_path),
        report.MarketMark(False), report.FeeComponent(False, None, "fee missing"),
        prepared_events=events)
    assert metrics["fallback_count"] == 1
    assert metrics["fallback_rate"] == pytest.approx(1)
    assert metrics["fallback_cashflow_usd"] == pytest.approx(49)
    assert metrics["fallback_counterfactual_impact_usd"] == pytest.approx(-1.5)
    assert metrics["fallback_attribution_status"] == "complete"
    assert metrics["fallback_events"][0]["actual_event_cashflow_usd"] == pytest.approx(-1)
    assert "Normal execution contribution" in html_text
    assert "Execution Cost / Fallback" in html_text
    assert "html-fallback" in html_text
    assert "Signal age ms</th>" in html_text
    assert "Block stage</th>" in html_text
    assert "Exact guard reason</th>" in html_text
    assert "RH nonce wait ms</th>" in html_text
    assert "Time between guard #1 and #2</th>" in html_text
    assert "Transport attempted</th>" in html_text
    assert "venue_pre_submit" in html_text
    assert "stale_or_pretrade_book" in html_text
    assert "6.25" in html_text
    assert "7.5" in html_text


def test_fallback_diagnostics_parse_new_columns_and_keep_legacy_unknown():
    legacy = _trade_row("legacy-diagnostic", buy_fill="0.5", sell_fill="0",
        sell_avg_px="", hedge_fill="0.5", hedge_avg_px="98",
        hedge_venue="hedge", hedge_side="sell", hedge_status="settled")
    events, _, anomalies = report.build_fill_ledger([legacy])
    assert not anomalies
    legacy_attr = report.build_execution_attributions(events)[0]
    assert legacy_attr["block_stage"] == "UNKNOWN"
    assert legacy_attr["exact_guard_reason"] == "UNKNOWN"
    assert legacy_attr["rh_nonce_wait_ms"] == "UNKNOWN"
    assert legacy_attr["pre_submit_wait_ms"] == "UNKNOWN"
    assert legacy_attr["transport_attempted"] == "UNKNOWN"

    diagnosed = dict(legacy)
    diagnosed.update({
        "sell_guard_trace": __import__("json").dumps([
            {"stage": "engine_preflight", "ok": True, "reason": "ok"},
            {"stage": "venue_pre_submit", "ok": False,
             "reason": "stale_signal"},
        ], separators=(",", ":")),
        "sell_transport_attempted": "0",
        "buy_transport_attempted": "1",
        "sell_pre_submit_wait_ms": "7.5",
        "sell_nonce_wait_ms": "6.25",
    })
    events, _, anomalies = report.build_fill_ledger([diagnosed])
    assert not anomalies
    item = report.build_execution_attributions(events)[0]
    assert item["block_stage"] == "venue_pre_submit"
    assert item["exact_guard_reason"] == "stale_signal"
    assert item["rh_nonce_wait_ms"] == pytest.approx(6.25)
    assert item["pre_submit_wait_ms"] == pytest.approx(7.5)
    assert item["transport_attempted"] is True


def test_flat_cycle_metrics_and_cycle_csv_emit_only_completed_cycles(tmp_path):
    build = _event("b1", 100, "buy_entropy", False, [
        _fill("b1:buy", "b1", 100, "entropy", "buy", 2, 100, 0),
        _fill("b1:sell", "b1", 100, "hedge", "sell", 2, 101, 0)], 2)
    release = _event("r1", 200, "buy_entropy", True, [
        _fill("r1:sell", "r1", 200, "entropy", "sell", 2, 102, 0),
        _fill("r1:buy", "r1", 200, "hedge", "buy", 2, 100, 0)], 2)
    state = {"entropy_qty": 0, "hedge_qty": 0, "mean_cost_per_base": None,
             "pending_intent": None}
    result = report.account_events([build, release], state, report.MarketMark(False))
    assert len(result.completed_cycles) == 1
    cycle = result.completed_cycles[0]
    assert cycle["gross_spread_capture_usd"] == pytest.approx(6)
    assert cycle["realized_pnl_before_funding_usd"] == pytest.approx(6)
    assert cycle["fees_usd"] == 0
    assert cycle["release_count"] == 1
    assert not result.current_cycle

    inputs = _inputs(tmp_path)
    snapshot = report.Snapshot(state, {"halted": True}, [], [], {}, consistent=True)
    metrics, _, cycles_csv = report.build_report(snapshot, inputs, report.MarketMark(False),
        report.FeeComponent(True, 0, "no actual fills"),
        prepared_events=[build, release])
    parsed = __import__("csv").DictReader(__import__("io").StringIO(cycles_csv))
    rows = list(parsed)
    assert parsed.fieldnames == report.CYCLE_COLUMNS
    assert len(rows) == 1
    assert metrics["completed_cycles"] == 1


def test_open_cycle_not_emitted_and_recorder_curve_keeps_invalid_quote_gaps():
    build = _event("b1", 100, "buy_entropy", False, [
        _fill("b1:buy", "b1", 100, "entropy", "buy", 1, 100, 0),
        _fill("b1:sell", "b1", 100, "hedge", "sell", 1, 101, 0)], 1)
    state = {"entropy_qty": 1, "hedge_qty": -1, "mean_cost_per_base": -1,
             "pending_intent": None}
    rows = [
        {"minute_ts": "120", "entropy_bid": "100", "entropy_ask": "101",
         "hedge_bid": "100", "hedge_ask": "101", "samples": "60"},
        {"minute_ts": "180", "entropy_bid": "", "entropy_ask": "101",
         "hedge_bid": "100", "hedge_ask": "101", "samples": "60"},
    ]
    curve = report.build_recorder_curve(rows, [build])
    assert curve[0]["inventory_usd"] == pytest.approx(100.5)
    assert curve[0]["gross_pnl"] is not None
    assert curve[1]["gross_pnl"] is None
    result = report.account_events([build], state, report.MarketMark(False))
    assert result.completed_cycles == []
    assert result.current_cycle["direction"] == "LONG"


def test_offline_cli_never_calls_network_and_writes_only_requested_outputs(tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "logs/state/range-ANTH-lighter-rh.json"
    halt_path = tmp_path / "logs/state/halt-ANTH-lighter-rh.json"
    trades_path = tmp_path / "logs/trades/trades-ANTH-lighter-rh.csv"
    recorder_path = tmp_path / "minutes.csv"
    state_path.parent.mkdir(parents=True)
    halt_path.write_text('{"halted":true,"timestamp":"t"}')
    state_path.write_text(__import__("json").dumps({
        "entropy_qty": 0, "hedge_qty": 0, "mean_cost_per_base": None,
        "pending_intent": None, "consumed_minute_ts": None,
        "current_target_usd": 0, "current_direction": None,
        "signal_metadata": None, "parameters": {"hard_cap_usd": 1500}}))
    trades_path.parent.mkdir(parents=True)
    trades_path.write_text("event_id,ts\n")
    recorder_path.write_text("symbol,hedge,minute_ts\n")
    (tmp_path / ".env").write_text("HL_PRIVATE_KEY=never-read\nHL_ACCOUNT_ADDRESS=public-address\n")
    def forbidden(*args, **kwargs):
        raise AssertionError("offline mode attempted network access")
    monkeypatch.setattr(report, "_http_json", forbidden)
    monkeypatch.setattr(report, "public_market_snapshot", forbidden)
    monkeypatch.setattr(report, "fetch_hl_fills", forbidden)
    code = report.main(["--symbol", "ANTH", "--hedge", "lighter-rh", "--root", str(tmp_path),
                        "--offline", "--recorder", str(recorder_path)])
    assert code == 0
    output = __import__("json").loads((tmp_path / "logs/performance/range-performance-ANTH-lighter-rh.json").read_text())
    assert output["funding_included"] is False
    assert output["unrealized_pnl_usd"] is None
    assert output["total_pnl_before_funding_usd"] is None
    assert sorted(p.name for p in (tmp_path / "logs/performance").iterdir()) == [
        "range-cycles-ANTH-lighter-rh.csv", "range-performance-ANTH-lighter-rh.html",
        "range-performance-ANTH-lighter-rh.json"]
    assert json.loads(capsys.readouterr().out)["snapshot_consistent"] is True


def _raw_hl(tid, oid, ts_ms, side="B", sz="1", px="100", fee="0.2", token="USD", coin="io:ANTH"):
    return {"tid": tid, "oid": oid, "time": ts_ms, "side": side, "sz": sz,
            "px": px, "fee": fee, "feeToken": token, "coin": coin, "hash": f"h{tid}"}


def test_hl_fee_match_unique_and_partial_fills_grouped_by_order():
    expected = [_fill("ev:buy", "ev", 10, "entropy", "buy", 1, 100)]
    raw = [_raw_hl(1, 77, 10000, sz="0.4", fee="0.1"),
           _raw_hl(2, 77, 10002, sz="0.6", fee="0.2")]
    match = report.match_entropy_fees(expected, raw, coin="io:ANTH")
    assert match.complete and match.matched == 1
    assert match.fees_by_fill_id["ev:buy"] == pytest.approx(0.3)


def test_hl_fee_match_missing_ambiguous_conflict_and_non_usd_fail_closed():
    expected = [_fill("ev:buy", "ev", 10, "entropy", "buy", 1, 100)]
    assert not report.match_entropy_fees(expected, [], coin="io:ANTH").complete
    ambiguous = [_raw_hl(1, 1, 10000), _raw_hl(2, 2, 10001)]
    assert not report.match_entropy_fees(expected, ambiguous, coin="io:ANTH").complete
    conflict = [_raw_hl(1, 1, 10000), _raw_hl(1, 1, 10000, fee="0.4")]
    assert not report.match_entropy_fees(expected, conflict, coin="io:ANTH").complete
    bad_currency = [_raw_hl(1, 1, 10000, token="USDC")]
    assert not report.match_entropy_fees(expected, bad_currency, coin="io:ANTH").complete
    assert not report.match_entropy_fees(expected, [_raw_hl(1, 1, 10000)],
                                         coin="io:ANTH", coverage_complete=False).complete


@pytest.mark.parametrize("metadata,expected", [
    ({"taker_fee": 0, "fee_mechanism_active": False}, True),
    ({"taker_fee": 0}, False),
    ({"taker_fee": 0.001, "fee_mechanism_active": False}, False),
    ({"taker_fee": 0, "fee_active": True}, False),
])
def test_rh_zero_fee_requires_current_official_zero_and_inactive_mechanism(metadata, expected):
    assert report.resolve_rh_fee(metadata).complete is expected


def test_hl_fee_is_not_double_counted_with_builder_fee():
    expected = [_fill("ev:buy", "ev", 10, "entropy", "buy", 1, 100)]
    raw = [_raw_hl(1, 1, 10000, fee="0.25") | {"builderFee": "0.1"}]
    match = report.match_entropy_fees(expected, raw, coin="io:ANTH")
    assert match.fees_by_fill_id["ev:buy"] == pytest.approx(0.25)


def test_hl_fetch_partitions_capped_response_and_marks_minimum_capped_incomplete():
    calls = []
    def responder(body):
        calls.append(body)
        if body["endTime"] - body["startTime"] >= 0:
            return [{}] * report.HL_RESPONSE_CAP
    rows, complete, reason = report.fetch_hl_fills("0xpublic", 0, 4, post_json=responder)
    assert not complete and "minimum" in reason
    assert len(calls) > 1


def test_fee_net_pnl_remains_null_if_any_actual_fee_is_unknown(tmp_path):
    inputs = _inputs(tmp_path)
    event = {"event_id": "open", "ts": "100", "signal_ts": "99", "direction": "buy_entropy",
        "reduce_only": "0", "matched_qty": "1", "buy_venue": "ENTROPY", "sell_venue": "RH",
        "buy_fill": "1", "buy_avg_px": "100", "sell_fill": "1", "sell_avg_px": "101",
        "hedge_fill": "0", "unresolved": "0"}
    state = _flat_state(entropy_qty=1, hedge_qty=-1, mean_cost_per_base=-1)
    snapshot = report.Snapshot(state, {"halted": True}, [event], [], {}, consistent=True)
    metrics, _, _ = report.build_report(snapshot, inputs, report.MarketMark(False),
        report.FeeComponent(False, None, "actual fee unavailable"),
        hl_match=report.FeeMatchResult(False, {}, 0, 1, "offline"))
    assert metrics["gross_realized_pnl_usd"] == 0
    assert metrics["gross_unrealized_pnl_usd"] is None
    assert metrics["fees_usd"] is None
    assert metrics["total_pnl_before_funding_usd"] is None


def test_snapshot_retry_exhaustion_marks_current_valuation_na():
    snapshot = report.Snapshot({}, {}, [], [], {}, attempts=3, consistent=False,
                               reason="inconsistent_snapshot: state changed")
    assert "inconsistent_snapshot" in snapshot.reason
    assert not snapshot.consistent


def test_renderer_escapes_artifact_text_and_has_no_external_assets():
    metrics = {"gross_total_pnl_usd": None, "fees_usd": None,
        "total_pnl_before_funding_usd": None, "gross_realized_pnl_usd": 0,
        "fee_net_realized_pnl_usd": None, "gross_unrealized_pnl_usd": None,
        "unrealized_pnl_usd": None, "current_direction": "<script>alert(1)</script>",
        "current_open_cycle": {"direction": "<script>"}, "current_inventory_usd": None,
        "cap_usd": 1500, "peak_inventory_usd": None, "current_cycle_return_pct": None,
        "build_count": 0, "release_count": 0, "execution_count": 0,
        "fallback_count": 0, "unresolved_count": 0, "long_percentile": None,
        "short_percentile": None, "range_4h_bps": None, "range_gate_open": None,
        "current_target_usd": 0, "current_net_delta": 0, "halted": True}
    text = report.render_html(metrics, [], [], fee_status={"status":"incomplete","reason":"<bad>"},
        mark=report.MarketMark(False), symbol="ANTH", hedge="lighter-rh")
    assert "<script>alert" not in text
    assert "&lt;script&gt;" in text
    assert "https://" not in text and "<script src" not in text


def test_json_serialization_converts_nonfinite_to_null():
    assert report._json_safe({"nan": float("nan"), "inf": float("inf")}) == {"nan": None, "inf": None}


def test_ec2_regression_only_passes_with_all_28_matches_provenance_and_tolerance():
    archive = [{"flat": True} for _ in range(28)]
    complete = report.FeeMatchResult(True, {}, 28, 28)
    passed = report.evaluate_ec2_regression(archive, complete, True, 0.261158)
    assert passed["status"] == "PASS"
    incomplete = report.evaluate_ec2_regression(archive,
        report.FeeMatchResult(False, {}, 27, 28, "ambiguous"), True, 0.261158)
    assert incomplete["status"] == "NOT VERIFIABLE"
    no_provenance = report.evaluate_ec2_regression(archive, complete, False, 0.261158)
    assert no_provenance["status"] == "NOT VERIFIABLE"
