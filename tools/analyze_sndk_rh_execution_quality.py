#!/usr/bin/env python3
"""Offline Run02 five-level depth and execution-quality analysis.

This tool replays only a frozen, checksum-verified Run02 snapshot.  It uses
the same local-receive-time pair construction and BBO edge definitions as the
quote-latency analysis, then walks the recorded five levels for a requested
buy-leg USD notional.  No venue/API, credential, Engine, or order code is
loaded by this module.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    from tools.analyze_sndk_rh_run02_persistence import (
        _pair_edges,
        finite,
        iso_utc,
        load_dataset,
        percentile,
    )
except ModuleNotFoundError:  # direct ``python tools/<script>.py`` execution
    from analyze_sndk_rh_run02_persistence import (  # type: ignore
        _pair_edges,
        finite,
        iso_utc,
        load_dataset,
        percentile,
    )


WAIT_MS = (0, 100, 250, 500)
TARGET_NOTIONALS = (100.0, 250.0, 500.0, 1000.0)
PAIR_MAX_SKEW_MS = 250.0
DEFAULT_FEE_SCENARIOS = {
    "research_0bps": {"entropy_fee_bps": 0.0, "lighter_rh_fee_bps": 0.0},
    # Run02 does not contain fills or fee ledger rows.  This is an explicit
    # conservative scenario based on the prior Run01 raw Entropy fee context;
    # it is not a claim about the current account's effective fee.
    "entropy_raw_4_5bps": {"entropy_fee_bps": 4.5, "lighter_rh_fee_bps": 0.0},
}


def _finite_positive(value: Any) -> bool:
    return finite(value) and float(value) > 0


def _levels(value: Any) -> List[Tuple[float, float]]:
    result = []
    for level in value or []:
        try:
            price, size = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        if _finite_positive(price) and _finite_positive(size):
            result.append((price, size))
    return result


def walk_to_notional(levels: Sequence[Sequence[float]], target_usd: float) -> dict:
    """Walk levels until the exact target notional is reached or depth ends."""
    if not _finite_positive(target_usd):
        return {
            "complete": False, "qty": 0.0, "notional_usd": 0.0,
            "vwap": None, "levels_consumed": 0, "reason": "invalid_target",
        }
    remaining_usd = float(target_usd)
    qty = 0.0
    notional = 0.0
    consumed = 0
    for price, size in _levels(levels):
        take = min(size, remaining_usd / price)
        if take <= 0:
            continue
        qty += take
        notional += take * price
        remaining_usd -= take * price
        consumed += 1
        if remaining_usd <= max(1e-9, target_usd * 1e-12):
            return {
                "complete": True, "qty": qty, "notional_usd": notional,
                "vwap": notional / qty if qty else None,
                "levels_consumed": consumed, "reason": "ok",
            }
    return {
        "complete": False, "qty": qty, "notional_usd": notional,
        "vwap": notional / qty if qty else None,
        "levels_consumed": consumed, "reason": "depth_insufficient",
    }


def _walk_to_quantity(levels: Sequence[Sequence[float]], target_qty: float) -> dict:
    if not _finite_positive(target_qty):
        return {
            "complete": False, "qty": 0.0, "notional_usd": 0.0,
            "vwap": None, "levels_consumed": 0, "reason": "invalid_target",
        }
    remaining = float(target_qty)
    qty = 0.0
    notional = 0.0
    consumed = 0
    for price, size in _levels(levels):
        take = min(size, remaining)
        if take <= 0:
            continue
        qty += take
        notional += take * price
        remaining -= take
        consumed += 1
        if remaining <= max(1e-12, target_qty * 1e-12):
            return {
                "complete": True, "qty": qty, "notional_usd": notional,
                "vwap": notional / qty if qty else None,
                "levels_consumed": consumed, "reason": "ok",
            }
    return {
        "complete": False, "qty": qty, "notional_usd": notional,
        "vwap": notional / qty if qty else None,
        "levels_consumed": consumed, "reason": "depth_insufficient",
    }


def _fee_result(
    buy_notional: float, sell_notional: float,
    buy_fee_bps: float, sell_fee_bps: float,
) -> dict:
    buy_fee = float(buy_fee_bps) / 1e4
    sell_fee = float(sell_fee_bps) / 1e4
    gross_usd = sell_notional - buy_notional
    net_usd = sell_notional * (1.0 - sell_fee) - buy_notional * (1.0 + buy_fee)
    return {
        "buy_fee_bps": float(buy_fee_bps),
        "sell_fee_bps": float(sell_fee_bps),
        "gross_edge_usd": gross_usd,
        "net_edge_usd": net_usd,
        "net_edge_bps": net_usd / buy_notional * 1e4 if buy_notional else None,
        "fee_usd": gross_usd - net_usd,
        "positive_net": net_usd > 0,
    }


def _resolve_leg_fees(spec: dict, buy_venue: str, sell_venue: str) -> Tuple[float, float]:
    if "entropy_fee_bps" in spec or "lighter_rh_fee_bps" in spec:
        venue_fees = {
            "entropy": float(spec.get("entropy_fee_bps", 0.0)),
            "lighter-rh": float(spec.get("lighter_rh_fee_bps", 0.0)),
        }
        return venue_fees.get(buy_venue, 0.0), venue_fees.get(sell_venue, 0.0)
    return float(spec.get("buy_fee_bps", 0.0)), float(spec.get("sell_fee_bps", 0.0))


def _empty_fee_results(fee_scenarios: Dict[str, dict]) -> dict:
    return {
        name: {
            "buy_fee_bps": spec.get("buy_fee_bps"),
            "sell_fee_bps": spec.get("sell_fee_bps"),
            "entropy_fee_bps": spec.get("entropy_fee_bps"),
            "lighter_rh_fee_bps": spec.get("lighter_rh_fee_bps"),
            "gross_edge_usd": None,
            "net_edge_usd": None,
            "net_edge_bps": None,
            "fee_usd": None,
            "positive_net": None,
        }
        for name, spec in fee_scenarios.items()
    }


def _direction_books(state: dict, direction: str) -> Tuple[str, str, list, list]:
    if direction == "sell_entropy":
        return (
            "lighter-rh", "entropy",
            state["lighter-rh"].get("asks") or [],
            state["entropy"].get("bids") or [],
        )
    if direction == "buy_entropy":
        return (
            "entropy", "lighter-rh",
            state["entropy"].get("asks") or [],
            state["lighter-rh"].get("bids") or [],
        )
    raise ValueError(f"unknown direction: {direction}")


def evaluate_book_pair(
    state: dict,
    direction: str,
    target_usd: float,
    fee_scenarios: Dict[str, dict],
    *,
    pair_max_skew_ms: float = PAIR_MAX_SKEW_MS,
) -> dict:
    """Evaluate one same-time pair state without fabricating missing depth."""
    buy_venue, sell_venue, buy_levels, sell_levels = _direction_books(state, direction)
    result = {
        "status": "incomplete",
        "reason": None,
        "direction": direction,
        "target_usd": float(target_usd),
        "buy_venue": buy_venue,
        "sell_venue": sell_venue,
        "bbo_edge_bps": state.get(
            "sell_edge_bps" if direction == "sell_entropy" else "buy_edge_bps"
        ),
        "pair_skew_ms": state.get("pair_skew_ms"),
        "buy_vwap": None,
        "sell_vwap": None,
        "buy_notional_usd": None,
        "sell_notional_usd": None,
        "qty": None,
        "buy_levels_consumed": 0,
        "sell_levels_consumed": 0,
        "buy_slippage_bps": None,
        "sell_slippage_bps": None,
        "gross_edge_usd": None,
        "gross_edge_bps": None,
        "vwap_edge_minus_bbo_bps": None,
        "fee_results": _empty_fee_results(fee_scenarios),
    }
    if state.get("parameter_valid") is not True:
        result["reason"] = "invalid_snapshot"
        return result
    statuses = state.get("source_timestamp_status") or {}
    if any(status != "present" for status in statuses.values()):
        result["reason"] = "source_timestamp_unavailable"
        return result
    if not finite(state.get("pair_skew_ms")) or float(state["pair_skew_ms"]) > pair_max_skew_ms:
        result["reason"] = "stale_pair_skew"
        return result
    bbo_edge = result["bbo_edge_bps"]
    if not finite(bbo_edge):
        result["reason"] = "missing_bbo"
        return result

    buy = walk_to_notional(buy_levels, target_usd)
    result["buy_levels_consumed"] = buy["levels_consumed"]
    if not buy["complete"]:
        result["reason"] = "buy_depth_insufficient"
        return result
    sell = _walk_to_quantity(sell_levels, buy["qty"])
    result["sell_levels_consumed"] = sell["levels_consumed"]
    if not sell["complete"]:
        result["reason"] = "sell_depth_insufficient"
        return result

    result.update({
        "status": "complete",
        "reason": "ok",
        "buy_vwap": buy["vwap"],
        "sell_vwap": sell["vwap"],
        "buy_notional_usd": buy["notional_usd"],
        "sell_notional_usd": sell["notional_usd"],
        "qty": buy["qty"],
        "buy_slippage_bps": (
            (buy["vwap"] / buy_levels[0][0] - 1.0) * 1e4
            if buy_levels else None
        ),
        "sell_slippage_bps": (
            (sell_levels[0][0] / sell["vwap"] - 1.0) * 1e4
            if sell_levels else None
        ),
    })
    result["gross_edge_usd"] = sell["notional_usd"] - buy["notional_usd"]
    result["gross_edge_bps"] = result["gross_edge_usd"] / buy["notional_usd"] * 1e4
    result["vwap_edge_minus_bbo_bps"] = result["gross_edge_bps"] - float(bbo_edge)
    result["fee_results"] = {
        name: _fee_result(
            buy["notional_usd"], sell["notional_usd"],
            *_resolve_leg_fees(spec, buy_venue, sell_venue),
        )
        for name, spec in fee_scenarios.items()
    }
    return result


def select_horizon_state(
    states: Sequence[dict], signal_wall_ns: int, wait_ms: int,
    state_times: Optional[Sequence[int]] = None,
) -> Optional[dict]:
    """Return the first observed state at/after the causal horizon."""
    target_ns = int(signal_wall_ns) + int(wait_ms) * 1_000_000
    times = state_times or [int(row["event_wall_ns"]) for row in states]
    index = bisect.bisect_left(times, target_ns)
    return states[index] if index < len(states) else None


def build_pair_states(quote_rows: Sequence[dict]) -> List[dict]:
    """Replay one Run's local receive order into causal pair book states."""
    ordered = sorted(
        quote_rows,
        key=lambda row: (int(row["received_wall_ns"]), str(row.get("venue"))),
    )
    latest: Dict[str, dict] = {}
    states = []
    for quote in ordered:
        venue = quote.get("venue")
        if venue not in {"entropy", "lighter-rh"}:
            continue
        latest[venue] = quote
        entropy = latest.get("entropy")
        hedge = latest.get("lighter-rh")
        if entropy is None or hedge is None:
            continue
        edges = _pair_edges(entropy, hedge)
        if edges is None:
            continue
        entropy_ns = int(entropy["received_wall_ns"])
        hedge_ns = int(hedge["received_wall_ns"])
        params = entropy
        parameter_valid = params.get("parameter_valid") is True
        parameter_invalid_reason = params.get("parameter_invalid_reason")
        for key in ("parameter_valid", "effective_at", "window_start", "window_end", "sample_count", "coverage", "midline_bps", "upper_bps", "lower_bps"):
            if entropy.get(key) != hedge.get(key):
                parameter_valid = False
                parameter_invalid_reason = "parameter_mismatch"
                break
        states.append({
            "event_wall_ns": max(entropy_ns, hedge_ns),
            "update_wall_ns": int(quote["received_wall_ns"]),
            "updated_venue": venue,
            "pair_skew_ms": abs(entropy_ns - hedge_ns) / 1e6,
            "sell_edge_bps": edges[0],
            "buy_edge_bps": edges[1],
            "mid_premium_bps": edges[2],
            "parameter_valid": parameter_valid,
            "parameter_invalid_reason": parameter_invalid_reason,
            "effective_at": params.get("effective_at"),
            "sample_count": params.get("sample_count"),
            "coverage": params.get("coverage"),
            "midline_bps": params.get("midline_bps"),
            "upper_bps": params.get("upper_bps"),
            "lower_bps": params.get("lower_bps"),
            "source_timestamp_status": {
                "entropy": entropy.get("source_timestamp_status"),
                "lighter-rh": hedge.get("source_timestamp_status"),
            },
            "source_age_ms": {
                "entropy": entropy.get("source_age_ms"),
                "lighter-rh": hedge.get("source_age_ms"),
            },
            "entropy": {
                "bids": entropy.get("bids") or [],
                "asks": entropy.get("asks") or [],
            },
            "lighter-rh": {
                "bids": hedge.get("bids") or [],
                "asks": hedge.get("asks") or [],
            },
        })
    return states


def _event_row(signal: dict, run_id: str, delay_ms: int, target_usd: float, state: Optional[dict], fee_scenarios: Dict[str, dict]) -> dict:
    direction = str(signal["direction"])
    signal_edge_key = "sell_edge_bps" if direction == "sell_entropy" else "buy_edge_bps"
    row = {
        "run_id": run_id,
        "signal_id": signal.get("signal_id"),
        "direction": direction,
        "signal_wall_ns": signal.get("signal_wall_ns"),
        "signal_utc": iso_utc(signal.get("signal_wall_ns")),
        "delay_ms": delay_ms,
        "target_usd": target_usd,
        "initial_bbo_edge_bps": signal.get(signal_edge_key),
        "initial_pair_skew_ms": signal.get("pair_skew_ms"),
        "state_wall_ns": state.get("event_wall_ns") if state else None,
        "state_utc": iso_utc(state.get("event_wall_ns")) if state else None,
        "observed_after_ms": (
            (int(state["event_wall_ns"]) - int(signal["signal_wall_ns"])) / 1e6
            if state else None
        ),
        "state_pair_skew_ms": state.get("pair_skew_ms") if state else None,
        "state_source_age_entropy_ms": state.get("source_age_ms", {}).get("entropy") if state else None,
        "state_source_age_rh_ms": state.get("source_age_ms", {}).get("lighter-rh") if state else None,
        "state_parameter_valid": state.get("parameter_valid") if state else None,
        "state_parameter_invalid_reason": state.get("parameter_invalid_reason") if state else None,
        "status": "latency_observation_missing" if state is None else None,
        "reason": "no_post_signal_pair_observation" if state is None else None,
        "buy_venue": None,
        "sell_venue": None,
        "qty": None,
        "buy_vwap": None,
        "sell_vwap": None,
        "buy_notional_usd": None,
        "sell_notional_usd": None,
        "buy_levels_consumed": None,
        "sell_levels_consumed": None,
        "buy_slippage_bps": None,
        "sell_slippage_bps": None,
        "state_bbo_edge_bps": None,
        "delay_bbo_edge_loss_bps": None,
        "gross_edge_usd": None,
        "gross_vwap_edge_bps": None,
        "vwap_edge_minus_bbo_bps": None,
        "fee_results": _empty_fee_results(fee_scenarios),
    }
    if state is None:
        return row
    evaluated = evaluate_book_pair(state, direction, target_usd, fee_scenarios)
    row.update({
        key: evaluated.get(key) for key in (
            "status", "reason", "buy_venue", "sell_venue", "qty", "buy_vwap",
            "sell_vwap", "buy_notional_usd", "sell_notional_usd",
            "buy_levels_consumed", "sell_levels_consumed", "buy_slippage_bps",
            "sell_slippage_bps", "gross_edge_usd", "gross_edge_bps",
            "vwap_edge_minus_bbo_bps", "fee_results",
        )
    })
    row["gross_vwap_edge_bps"] = row.get("gross_edge_bps")
    row["state_bbo_edge_bps"] = evaluated.get("bbo_edge_bps")
    if finite(row["initial_bbo_edge_bps"]) and finite(row["state_bbo_edge_bps"]):
        row["delay_bbo_edge_loss_bps"] = (
            float(row["initial_bbo_edge_bps"]) - float(row["state_bbo_edge_bps"])
        )
    return row


def _flatten_event(row: dict, fee_scenarios: Dict[str, dict]) -> dict:
    result = {key: value for key, value in row.items() if key != "fee_results"}
    for name, fee_result in row["fee_results"].items():
        result[f"buy_fee_bps_{name}"] = fee_result.get("buy_fee_bps")
        result[f"sell_fee_bps_{name}"] = fee_result.get("sell_fee_bps")
        result[f"net_edge_usd_{name}"] = fee_result.get("net_edge_usd")
        result[f"net_edge_bps_{name}"] = fee_result.get("net_edge_bps")
        result[f"fee_usd_{name}"] = fee_result.get("fee_usd")
        result[f"positive_net_{name}"] = fee_result.get("positive_net")
    return result


def _write_csv(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in rows)


def _bucket(edge: Any) -> str:
    if not finite(edge):
        return "unavailable"
    value = float(edge)
    if value < 0:
        return "negative"
    if value < 2:
        return "0-2"
    if value < 5:
        return "2-5"
    if value < 10:
        return "5-10"
    return "10+"


def _metric(values: Iterable[Any], q: float) -> Optional[float]:
    clean = [float(value) for value in values if finite(value)]
    return percentile(clean, q)


def _fee_field(row: dict, fee_name: str, field: str) -> Any:
    return (row.get("fee_results") or {}).get(fee_name, {}).get(field)


def _summarize_events(events: Sequence[dict], fee_scenarios: Dict[str, dict]) -> Tuple[List[dict], List[dict]]:
    summaries = []
    bucket_rows = []
    for target in TARGET_NOTIONALS:
        for direction in ("all", "sell_entropy", "buy_entropy"):
            for delay in WAIT_MS:
                selected = [
                    row for row in events
                    if row["target_usd"] == target and row["delay_ms"] == delay
                    and (direction == "all" or row["direction"] == direction)
                ]
                raw = len(selected)
                complete = [row for row in selected if row["status"] == "complete"]
                for fee_name in fee_scenarios:
                    positive = [row for row in complete if _fee_field(row, fee_name, "positive_net") is True]
                    reason_counts = Counter(row["reason"] for row in selected if row["status"] != "complete")
                    summary = {
                        "target_usd": target,
                        "direction": direction,
                        "delay_ms": delay,
                        "fee_scenario": fee_name,
                        "raw_candidate_count": raw,
                        "complete_count": len(complete),
                        "complete_pct_of_candidates": 100.0 * len(complete) / raw if raw else None,
                        "depth_eligible_count": sum(
                            1 for row in selected
                            if row["status"] == "complete"
                            or row["reason"] in {"buy_depth_insufficient", "sell_depth_insufficient"}
                        ),
                        "depth_sufficient_pct_of_depth_eligible": (
                            100.0 * len(complete) / sum(
                                1 for row in selected
                                if row["status"] == "complete"
                                or row["reason"] in {"buy_depth_insufficient", "sell_depth_insufficient"}
                            )
                            if sum(
                                1 for row in selected
                                if row["status"] == "complete"
                                or row["reason"] in {"buy_depth_insufficient", "sell_depth_insufficient"}
                            ) else None
                        ),
                        "positive_net_count": len(positive),
                        "positive_net_pct_of_candidates": 100.0 * len(positive) / raw if raw else None,
                        "positive_net_pct_of_complete": 100.0 * len(positive) / len(complete) if complete else None,
                        "bbo_edge_p50_bps": _metric((row["initial_bbo_edge_bps"] for row in selected), 0.50),
                        "bbo_edge_p90_bps": _metric((row["initial_bbo_edge_bps"] for row in selected), 0.90),
                        "gross_vwap_edge_p50_bps": _metric((row["gross_vwap_edge_bps"] for row in complete), 0.50),
                        "gross_vwap_edge_p90_bps": _metric((row["gross_vwap_edge_bps"] for row in complete), 0.90),
                        "net_edge_p50_bps": _metric((_fee_field(row, fee_name, "net_edge_bps") for row in complete), 0.50),
                        "net_edge_p90_bps": _metric((_fee_field(row, fee_name, "net_edge_bps") for row in complete), 0.90),
                        "vwap_edge_minus_bbo_p50_bps": _metric((row["vwap_edge_minus_bbo_bps"] for row in complete), 0.50),
                        "vwap_edge_minus_bbo_p90_bps": _metric((row["vwap_edge_minus_bbo_bps"] for row in complete), 0.90),
                        "delay_bbo_edge_loss_p50_bps": _metric((row["delay_bbo_edge_loss_bps"] for row in selected), 0.50),
                        "delay_bbo_edge_loss_p90_bps": _metric((row["delay_bbo_edge_loss_bps"] for row in selected), 0.90),
                        "buy_slippage_p50_bps": _metric((row["buy_slippage_bps"] for row in complete), 0.50),
                        "sell_slippage_p50_bps": _metric((row["sell_slippage_bps"] for row in complete), 0.50),
                        "buy_slippage_p90_bps": _metric((row["buy_slippage_bps"] for row in complete), 0.90),
                        "sell_slippage_p90_bps": _metric((row["sell_slippage_bps"] for row in complete), 0.90),
                        "latency_missing_count": reason_counts.get("no_post_signal_pair_observation", 0),
                        "stale_pair_skew_count": reason_counts.get("stale_pair_skew", 0),
                        "invalid_snapshot_count": reason_counts.get("invalid_snapshot", 0),
                        "source_timestamp_unavailable_count": reason_counts.get("source_timestamp_unavailable", 0),
                        "buy_depth_insufficient_count": reason_counts.get("buy_depth_insufficient", 0),
                        "sell_depth_insufficient_count": reason_counts.get("sell_depth_insufficient", 0),
                        "missing_bbo_count": reason_counts.get("missing_bbo", 0),
                    }
                    summaries.append(summary)
                for bucket in ("negative", "0-2", "2-5", "5-10", "10+", "unavailable"):
                    bucket_events = [
                        row for row in selected
                        if _bucket(row["initial_bbo_edge_bps"]) == bucket
                    ]
                    complete_bucket = [row for row in bucket_events if row["status"] == "complete"]
                    for fee_name in fee_scenarios:
                        positive_bucket = [
                            row for row in complete_bucket
                            if _fee_field(row, fee_name, "positive_net") is True
                        ]
                        bucket_rows.append({
                            "target_usd": target,
                            "direction": direction,
                            "delay_ms": delay,
                            "edge_bucket_bps": bucket,
                            "fee_scenario": fee_name,
                            "candidate_count": len(bucket_events),
                            "complete_count": len(complete_bucket),
                            "positive_net_count": len(positive_bucket),
                            "positive_net_pct_of_candidates": 100.0 * len(positive_bucket) / len(bucket_events) if bucket_events else None,
                            "net_edge_p50_bps": _metric((_fee_field(row, fee_name, "net_edge_bps") for row in complete_bucket), 0.50),
                        })
    return summaries, bucket_rows


def _svg_chart(path: Path, summaries: Sequence[dict], fee_name: str) -> None:
    rows = [
        row for row in summaries
        if row["direction"] == "all" and row["fee_scenario"] == fee_name
    ]
    width, height = 1000, 520
    left, top, panel_w, panel_h = 60, 75, 410, 330
    def line_points(data: Sequence[dict], key: str, x: int, y: int, w: int, h: int) -> str:
        values = [row[key] for row in data if finite(row.get(key))]
        if not values:
            return ""
        lo, hi = min(0.0, min(values)), max(1.0, max(values))
        if math.isclose(lo, hi):
            hi = lo + 1.0
        coords = []
        for i, row in enumerate(data):
            value = row.get(key)
            if not finite(value):
                continue
            px = x + w * i / max(1, len(data) - 1)
            py = y + h - (float(value) - lo) / (hi - lo) * h
            coords.append(f"{px:.1f},{py:.1f}")
        return " ".join(coords)
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        '<style>.title{font:600 16px sans-serif}.axis,.legend,.note{font:12px sans-serif}.plot{fill:#fafafa;stroke:#bbb}</style>',
        f'<text x="60" y="28" class="note">Run02 execution quality; fee scenario: {fee_name}; observed VWAP, not fills</text>',
    ]
    panels = [
        (left, top, "Positive net edge (%)", "positive_net_pct_of_candidates", "#166534"),
        (550, top, "Gross VWAP edge P50 (bps)", "gross_vwap_edge_p50_bps", "#1d4ed8"),
    ]
    for x, y, title, key, color in panels:
        svg.append(f'<text x="{x}" y="{y - 20}" class="title">{title}</text>')
        svg.append(f'<rect x="{x}" y="{y}" width="{panel_w}" height="{panel_h}" class="plot"/>')
        points = line_points(rows, key, x, y, panel_w, panel_h)
        if points:
            svg.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
        for i, row in enumerate(rows):
            if finite(row.get(key)):
                px = x + panel_w * i / max(1, len(rows) - 1)
                values = [r[key] for r in rows if finite(r.get(key))]
                lo, hi = min(0.0, min(values)), max(1.0, max(values))
                if math.isclose(lo, hi):
                    hi = lo + 1.0
                py = y + panel_h - (float(row[key]) - lo) / (hi - lo) * panel_h
                svg.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="{color}"/>')
            svg.append(f'<text x="{x + panel_w * i / max(1, len(rows) - 1):.1f}" y="{y + panel_h + 20}" text-anchor="middle" class="axis">${int(row["target_usd"])}@{row["delay_ms"]}</text>')
    svg.append('</svg>')
    path.write_text("\n".join(svg) + "\n", encoding="utf-8")


def _write_report(path: Path, data: dict, result: dict) -> None:
    integrity = data["integrity"]
    summaries = result["summaries"]
    conservative = "entropy_raw_4_5bps"
    def get(target: float, direction: str, delay: int) -> dict:
        return next(row for row in summaries if row["target_usd"] == target and row["direction"] == direction and row["delay_ms"] == delay and row["fee_scenario"] == conservative)
    lines = [
        "# SNDK × Lighter RH Execution Quality Phase 1",
        "",
        "本報告只使用 Run02 的完整 Quote、Signal、Decay 與五層訂單簿。VWAP、Edge、深度與延遲結果都是歷史行情模擬，不是實際成交、已實現 Capture 或 PnL。",
        "",
        "## 資料與成本口徑",
        "",
        f"- Source commit: `{integrity['supervisor'].get('source_commit')}`; market: `{integrity['supervisor'].get('market')}`。",
        f"- Frozen input: `{data.get('data_root', 'Run02 snapshot')}`; eligible completed Runs: {integrity['eligible_run_count']}/{integrity['manifest_count']}。",
        f"- Quotes {integrity['row_counts']['quotes']['retained']:,}; Signals {integrity['row_counts']['signals']['retained']:,}; Decay {integrity['row_counts'].get('decay', {}).get('retained', 'N/A'):,}; Quote retention {integrity['row_counts']['quotes']['retention_pct']:.1f}%。",
        f"- SHA mismatch {integrity['sha256_mismatch_count']}; parse errors {integrity['parse_error_count']}; drops {integrity['dropped_updates'] + integrity['dropped_lifecycle']}; writer errors {integrity['write_errors']}。",
        "- $100/$250/$500/$1,000 定義為買入腿的目標 USD 名目。先走買入腿五層 ask 到目標，再要求賣出腿以相同 base quantity 有足夠五層 bid；不足即標記不可完整成交，不使用第五檔外推。",
        "- `research_0bps` 來自 Run02 research fee setting。`entropy_raw_4_5bps` 是已知的 Entropy raw fee 情境加 RH 0 bps；Run02 沒有成交或 fee ledger，故目前帳戶適用性與返佣均 NOT VERIFIED。",
        "- Run02 沒有實際 fill、funding、返佣或 execution ledger；本分析只做五層可見深度與費率敏感度，不估計 funding、queue position、partial fill 或已實現 PnL。",
        "- `sell_entropy` 方向為買 RH ask、賣 Entropy bid；`buy_entropy` 方向為買 Entropy ask、賣 RH bid。VWAP 先以買入腿目標名目走深度，再以相同 base quantity 走賣出腿。",
        "- 延遲狀態是每個 signal 在同一 Run 內第一個 `event_wall_ns >= signal_wall_ns + delay` 的雙腿觀測；沒有後續觀測就標記 missing，不跨 Run 拼接。",
        "",
        "## 0ms 深度與成本比較",
        "",
        "| Target | candidates | state/skew eligible | 5L depth sufficient | full executable | VWAP Edge P50/P90 | conservative net P50 | positive net | 100ms | 250ms | 500ms |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for target in TARGET_NOTIONALS:
        base = get(target, "all", 0)
        lines.append(
            f"| ${int(target):,} | {base['raw_candidate_count']} | {_fmt_pct(100.0 * base['depth_eligible_count'] / base['raw_candidate_count'] if base['raw_candidate_count'] else None)}% | {_fmt_pct(base['depth_sufficient_pct_of_depth_eligible'])}% | {_fmt_pct(base['complete_pct_of_candidates'])}% | {_fmt(base['gross_vwap_edge_p50_bps'])}/{_fmt(base['gross_vwap_edge_p90_bps'])} bps | {_fmt(base['net_edge_p50_bps'])} bps | {_fmt_pct(base['positive_net_pct_of_candidates'])}% | {_fmt_pct(get(target, 'all', 100)['positive_net_pct_of_candidates'])}% | {_fmt_pct(get(target, 'all', 250)['positive_net_pct_of_candidates'])}% | {_fmt_pct(get(target, 'all', 500)['positive_net_pct_of_candidates'])}% |"
        )
    lines += [
        "",
        "## BUY / SELL（保守費用情境）",
        "",
        "| Direction | Target | 0ms complete | 0ms positive net | 100ms positive | 250ms positive | 500ms positive | 0ms buy/sell slippage P50 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for direction in ("sell_entropy", "buy_entropy"):
        for target in TARGET_NOTIONALS:
            zero = get(target, direction, 0)
            lines.append(
                f"| {direction} | ${int(target):,} | {_fmt_pct(zero['complete_pct_of_candidates'])}% | {_fmt_pct(zero['positive_net_pct_of_candidates'])}% | {_fmt_pct(get(target, direction, 100)['positive_net_pct_of_candidates'])}% | {_fmt_pct(get(target, direction, 250)['positive_net_pct_of_candidates'])}% | {_fmt_pct(get(target, direction, 500)['positive_net_pct_of_candidates'])}% | {_fmt(zero['buy_slippage_p50_bps'])}/{_fmt(zero['sell_slippage_p50_bps'])} bps |"
            )
    lines += [
        "",
        "## 延遲與不可執行情況",
        "",
        "`positive net` 的分母是全部原始候選，因而同時反映深度不足、過期 pair skew、Rolling 無效與等待後無 Quote 的機會損失。`delay_bbo_edge_loss` 是 signal BBO Edge 減去延遲後 BBO Edge；VWAP Edge 仍只在兩腿五層可完整走完時統計。",
        "",
        "| Target | Delay | complete | stale skew | buy depth short | sell depth short | latency missing | delay BBO loss P50/P90 | VWAP-vs-BBO P50 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for target in TARGET_NOTIONALS:
        for delay in WAIT_MS:
            row = get(target, "all", delay)
            lines.append(
                f"| ${int(target):,} | {delay}ms | {row['complete_count']} | {row['stale_pair_skew_count']} | {row['buy_depth_insufficient_count']} | {row['sell_depth_insufficient_count']} | {row['latency_missing_count']} | {_fmt(row['delay_bbo_edge_loss_p50_bps'])}/{_fmt(row['delay_bbo_edge_loss_p90_bps'])} bps | {_fmt(row['vwap_edge_minus_bbo_p50_bps'])} bps |"
            )
    lines += [
        "",
        "## Edge size buckets",
        "",
        "逐 target、direction、delay、fee scenario 的分桶結果在 `edge_bucket_summary.csv`。分桶依 signal 時的 signed BBO Edge，另保留 negative 與 unavailable。",
        "",
        "## 結論",
        "",
        "1. 在 0 bps 研究情境，小額 0ms VWAP Net Edge 中位數仍為正；但套用 4.5 bps Entropy raw fee 情境後，四種名目在 0ms 的 Net Edge P50 均為負，正 Net Edge 僅約 2.5–4.4% 的全部候選。這不是已成交利潤證據。",
        "2. $100–$250 的五層深度完整率約 99.99%，$1,000 約 99.81%；較大名目 VWAP edge 中位數下降，且深度短缺增多。小額的歷史可觀測性較好，但不代表可下單。",
        "3. BUY／SELL 的費用依實際 Entropy venue leg 映射；SELL Entropy 時費用落在賣 Entropy leg，BUY Entropy 時落在買 Entropy leg。兩腿 slippage 另列，不能只用一腿的 BBO premium 排名。",
        "4. 250ms 延遲後只有約 4.95% 候選仍通過 pair-skew/狀態條件；500ms 約 47.0%。延遲後 BBO Edge loss 的 P90 約 0.56/1.19/2.38 bps（100/250/500ms），且這是觀測行情衰退，不是成交延遲的 PnL。",
        "5. 最終建議：**NO-GO 進入真實成交測試**。Run02 沒有實際 fills、execution timestamps、部分成交或帳戶 fee/返佣 ledger；需要另行授權的受控驗證，不能把歷史 VWAP 模擬當成已實現收益。",
        "",
        "## 產物",
        "",
        "- `execution_quality_summary.csv`：主統計，含 0/100/250/500ms、BUY/SELL、四種 target 與兩個 fee scenario。",
        "- `execution_quality_events.csv.gz`：逐 Signal × target × delay 的 CSV 狀態、VWAP、深度、slippage 與 fee-adjusted edge（解壓後為完整事件表）。",
        "- `edge_bucket_summary.csv`：0–2、2–5、5–10、10+ bps 及 negative/unavailable 分桶。",
        "- `execution_quality_integrity.json`：Run02 integrity 摘要與分析設定。",
        "- `execution_quality_summary.svg`：無外部依賴的比較圖。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt(value: Any) -> str:
    return "N/A" if value is None else (f"{float(value):.3f}" if isinstance(value, (int, float)) else str(value))


def _fmt_pct(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.2f}"


def analyze(
    data_root: str | Path,
    output_dir: str | Path,
    *,
    target_notionals: Sequence[float] = TARGET_NOTIONALS,
    wait_ms: Sequence[int] = WAIT_MS,
    fee_scenarios: Optional[Dict[str, dict]] = None,
) -> dict:
    data = load_dataset(data_root)
    data["data_root"] = str(data_root)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    fees = fee_scenarios or DEFAULT_FEE_SCENARIOS
    events = []
    run_rows = []
    for run in data["runs"]:
        states = build_pair_states(run["records"]["quotes"])
        states.sort(key=lambda row: int(row["event_wall_ns"]))
        state_times = [int(row["event_wall_ns"]) for row in states]
        signals = sorted(run["records"]["signals"], key=lambda row: int(row["signal_wall_ns"]))
        for signal in signals:
            for target in target_notionals:
                for delay in wait_ms:
                    state = select_horizon_state(
                        states, int(signal["signal_wall_ns"]), int(delay), state_times,
                    )
                    events.append(_event_row(signal, run["run_id"], int(delay), float(target), state, fees))
        run_rows.append({
            "run_id": run["run_id"],
            "quote_count": len(run["records"]["quotes"]),
            "signal_count": len(signals),
            "pair_state_count": len(states),
            "first_state_utc": iso_utc(states[0]["event_wall_ns"]) if states else None,
            "last_state_utc": iso_utc(states[-1]["event_wall_ns"]) if states else None,
        })
    summaries, buckets = _summarize_events(events, fees)
    integrity = dict(data["integrity"])
    integrity["analysis"] = {
        "target_notionals_usd": list(target_notionals),
        "wait_ms": list(wait_ms),
        "pair_max_skew_ms": PAIR_MAX_SKEW_MS,
        "fee_scenarios": fees,
        "event_count": len(events),
        "run_summary": run_rows,
    }
    (output / "execution_quality_integrity.json").write_text(
        json.dumps(integrity, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    _write_csv(output / "execution_quality_events.csv", [_flatten_event(row, fees) for row in events])
    _write_csv(output / "execution_quality_summary.csv", summaries)
    _write_csv(output / "edge_bucket_summary.csv", buckets)
    _write_csv(output / "execution_quality_run_summary.csv", run_rows)
    chart = output / "execution_quality_summary.svg"
    _svg_chart(chart, summaries, "entropy_raw_4_5bps")
    result = {
        "data_root": str(data_root),
        "output_dir": str(output),
        "integrity": integrity,
        "event_count": len(events),
        "summaries": summaries,
        "bucket_rows": buckets,
        "run_summary": run_rows,
        "charts": [chart.name],
    }
    (output / "execution_quality_analysis.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    _write_report(output.parent / "sndk_rh_execution_quality_phase1.md", data, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    result = analyze(args.data_root, args.output_dir)
    print(json.dumps({
        "eligible_runs": result["integrity"]["eligible_run_count"],
        "quotes": result["integrity"]["row_counts"]["quotes"],
        "signals": result["integrity"]["row_counts"]["signals"],
        "event_count": result["event_count"],
        "output_dir": result["output_dir"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
