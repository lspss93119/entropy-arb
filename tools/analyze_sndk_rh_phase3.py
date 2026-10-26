#!/usr/bin/env python3
"""Offline P1/P2/P3 analysis for retained SNDK x Lighter RH quote runs.

The analyzer consumes only checksum-matching completed runs from the Phase 3
backup manifest. It never imports the trading Engine and never changes source
data. Counterfactual skew and persistence results are explicitly limited to
the retained quote segments because the old max_files=2 runs are incomplete.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


HORIZONS_MS = (50, 100, 250, 500, 1000)
SKEW_THRESHOLDS_MS = (100.0, 250.0, 500.0)
PERSISTENCE_THRESHOLDS_MS = (100.0, 250.0, 500.0)
AGE_BUCKETS = (
    ("0-50", 0.0, 50.0),
    ("50-100", 50.0, 100.0),
    ("100-250", 100.0, 250.0),
    ("250-500", 250.0, 500.0),
    ("500+", 500.0, math.inf),
)


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Linear-interpolation percentile matching the research convention."""
    clean = sorted(float(v) for v in values if finite(v))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    position = (len(clean) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return clean[low]
    return clean[low] + (clean[high] - clean[low]) * (position - low)


def numeric_summary(values: Sequence[float]) -> Dict[str, Optional[float]]:
    clean = [float(v) for v in values if finite(v)]
    return {
        "n": len(clean),
        "p50": percentile(clean, 0.50),
        "p90": percentile(clean, 0.90),
        "p95": percentile(clean, 0.95),
        "p99": percentile(clean, 0.99),
        "max": max(clean) if clean else None,
        "mean": statistics.fmean(clean) if clean else None,
    }


def correlation(values_x: Sequence[float], values_y: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    pairs = [(float(x), float(y)) for x, y in zip(values_x, values_y)
             if finite(x) and finite(y)]
    if len(pairs) < 3:
        return None, None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    den = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    pearson = sum(x * y for x, y in zip(dx, dy)) / den if den else None

    def ranks(values: Sequence[float]) -> List[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        result = [0.0] * len(values)
        index = 0
        while index < len(order):
            end = index + 1
            while end < len(order) and values[order[end]] == values[order[index]]:
                end += 1
            rank = (index + end - 1) / 2.0 + 1.0
            for pos in order[index:end]:
                result[pos] = rank
            index = end
        return result

    rx = ranks(xs)
    ry = ranks(ys)
    mrx = statistics.fmean(rx)
    mry = statistics.fmean(ry)
    drx = [x - mrx for x in rx]
    dry = [y - mry for y in ry]
    rden = math.sqrt(sum(x * x for x in drx) * sum(y * y for y in dry))
    spearman = sum(x * y for x, y in zip(drx, dry)) / rden if rden else None
    return pearson, spearman


def age_bucket(age: Any) -> str:
    if not finite(age) or float(age) < 0:
        return "unknown"
    value = float(age)
    for label, lower, upper in AGE_BUCKETS:
        if lower <= value < upper:
            return label
    return "unknown"


def utc_hour_label(ns: int, block_hours: int = 4) -> str:
    dt = datetime.fromtimestamp(ns / 1e9, tz=timezone.utc)
    hour = (dt.hour // block_hours) * block_hours
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jsonl(path: Path, parse_errors: List[Dict[str, Any]]) -> List[dict]:
    rows: List[dict] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        rows.append(value)
                    else:
                        parse_errors.append({"path": str(path), "line": line_number, "reason": "not_object"})
                except json.JSONDecodeError as exc:
                    parse_errors.append({"path": str(path), "line": line_number, "reason": str(exc)})
    except OSError as exc:
        parse_errors.append({"path": str(path), "line": 0, "reason": str(exc)})
    return rows


def load_completed_runs(base_dir: str | Path) -> Tuple[List[dict], Dict[str, Any]]:
    """Select only runs whose manifest and every listed file passed checksum."""
    base = Path(base_dir)
    backup = json.loads((base / "backup_manifest.json").read_text(encoding="utf-8"))
    entries = {item["p"]: item for item in backup.get("files", [])}
    candidates = sorted({
        parts[1] for path in entries
        if path.startswith("runs/") and len(parts := path.split("/")) >= 3
    })
    runs: List[dict] = []
    excluded: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []

    for run_id in candidates:
        manifest_rel = f"runs/{run_id}/manifest.json"
        manifest_entry = entries.get(manifest_rel)
        if not manifest_entry or manifest_entry.get("match") is not True:
            excluded.append({"run_id": run_id, "reason": "manifest_checksum_not_verified"})
            continue
        run_paths = sorted(
            path for path in entries
            if path.startswith(f"runs/{run_id}/")
        )
        bad = [
            path for path in run_paths
            if entries[path].get("match") is not True or not (base / path).is_file()
        ]
        if bad:
            excluded.append({"run_id": run_id, "reason": "run_file_checksum_not_verified", "files": bad})
            continue
        try:
            run_manifest = json.loads((base / manifest_rel).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            excluded.append({"run_id": run_id, "reason": f"manifest_parse_error:{exc}"})
            continue
        if run_manifest.get("market") != "io:SNDK|lighter-rh:SNDK":
            excluded.append({"run_id": run_id, "reason": "market_mismatch"})
            continue

        records: Dict[str, List[dict]] = {
            "quotes": [], "signals": [], "decay": [], "lifecycle": []
        }
        for kind in records:
            for path in run_paths:
                filename = Path(path).name
                if not filename.startswith(f"{kind}-") or not filename.endswith(".jsonl"):
                    continue
                records[kind].extend(_jsonl(base / path, parse_errors))

        records["quotes"].sort(key=lambda row: (int(row.get("received_wall_ns", 0)), row.get("venue", "")))
        records["signals"].sort(key=lambda row: int(row.get("signal_wall_ns", 0)))
        records["decay"].sort(key=lambda row: (row.get("signal_id", ""), int(row.get("target_ms", 0))))
        writer_stats = run_manifest.get("writer_stats", {})
        runs.append({
            "run_id": run_id,
            "manifest": run_manifest,
            "records": records,
            "run_paths": run_paths,
            "quote_written": int(writer_stats.get("quotes", {}).get("rows_written", 0) or 0),
            "signal_written": int(writer_stats.get("signals", {}).get("rows_written", 0) or 0),
            "decay_written": int(writer_stats.get("decay", {}).get("rows_written", 0) or 0),
        })

    selection = {
        "backup_dir": str(base),
        "generated_at_utc": backup.get("generated_at_utc"),
        "remote_files": backup.get("remote_file_count"),
        "checksum_matches": backup.get("checksum_matches"),
        "checksum_mismatches": len(backup.get("checksum_mismatches", [])),
        "missing_local_files": len(backup.get("missing_local_files", [])),
        "completed_runs": len(runs),
        "excluded_runs": excluded,
        "parse_errors": parse_errors,
    }
    return runs, selection


def _bbo(row: dict) -> Optional[Tuple[float, float]]:
    bids = row.get("bids") or []
    asks = row.get("asks") or []
    if not bids or not asks:
        return None
    try:
        bid = float(bids[0][0])
        ask = float(asks[0][0])
    except (TypeError, ValueError, IndexError):
        return None
    if not (finite(bid) and finite(ask) and bid > 0 and ask > 0):
        return None
    return bid, ask


def _pair_edges(entropy: dict, hedge: dict) -> Optional[Tuple[float, float, float]]:
    e_bbo = _bbo(entropy)
    h_bbo = _bbo(hedge)
    if e_bbo is None or h_bbo is None:
        return None
    e_bid, e_ask = e_bbo
    h_bid, h_ask = h_bbo
    sell = (e_bid / h_ask - 1.0) * 1e4
    buy = (h_bid / e_ask - 1.0) * 1e4
    mid = (((e_bid + e_ask) / 2.0) / ((h_bid + h_ask) / 2.0) - 1.0) * 1e4
    if not all(finite(value) for value in (sell, buy, mid)):
        return None
    return sell, buy, mid


def _valid_parameters(row: dict) -> bool:
    if row.get("parameter_valid") is not True:
        return False
    return all(finite(row.get(name)) for name in ("midline_bps", "upper_bps", "lower_bps"))


def _observation(
    latest: Dict[str, dict],
    skew_threshold_ms: float,
    parameter_row: dict,
) -> Optional[dict]:
    entropy = latest.get("entropy")
    hedge = latest.get("lighter-rh")
    if entropy is None or hedge is None:
        return None
    edges = _pair_edges(entropy, hedge)
    if edges is None:
        return None
    event_wall_ns = max(int(entropy["received_wall_ns"]), int(hedge["received_wall_ns"]))
    skew_ms = abs(int(entropy["received_wall_ns"]) - int(hedge["received_wall_ns"])) / 1e6
    valid = _valid_parameters(parameter_row)
    params = parameter_row
    if valid:
        sell_threshold = float(params["midline_bps"]) + float(params["upper_bps"])
        buy_threshold = float(params["lower_bps"]) - float(params["midline_bps"])
    else:
        sell_threshold = buy_threshold = None
    return {
        "event_wall_ns": event_wall_ns,
        "pair_skew_ms": skew_ms,
        "sell_edge_bps": edges[0],
        "buy_edge_bps": edges[1],
        "mid_premium_bps": edges[2],
        "parameter_valid": valid,
        "eligible": bool(valid and skew_ms <= skew_threshold_ms),
        "sell_threshold_bps": sell_threshold,
        "buy_threshold_bps": buy_threshold,
    }


def replay_candidates(
    quote_rows: Sequence[dict],
    *,
    skew_threshold_ms: float,
    warmup_ms: float = 1000.0,
) -> Tuple[List[dict], List[dict]]:
    """Replay quote rows without crossing run boundaries or using future rows."""
    rows = sorted(quote_rows, key=lambda row: (int(row["received_wall_ns"]), row.get("venue", "")))
    if not rows:
        return [], []
    segment_start_ns = int(rows[0]["received_wall_ns"])
    warmup_cutoff_ns = segment_start_ns + int(warmup_ms * 1e6)
    latest: Dict[str, dict] = {}
    active = set()
    candidates: List[dict] = []
    observations: List[dict] = []

    for row in rows:
        venue = row.get("venue")
        if venue not in {"entropy", "lighter-rh"}:
            continue
        latest[venue] = row
        obs = _observation(latest, skew_threshold_ms, row)
        if obs is None:
            active.clear()
            continue
        observations.append(obs)
        if not obs["eligible"]:
            active.clear()
            continue
        thresholds = {
            "sell_entropy": obs["sell_threshold_bps"],
            "buy_entropy": obs["buy_threshold_bps"],
        }
        edges = {
            "sell_entropy": obs["sell_edge_bps"],
            "buy_entropy": obs["buy_edge_bps"],
        }
        for direction in ("sell_entropy", "buy_entropy"):
            edge = edges[direction]
            threshold = thresholds[direction]
            qualifies = finite(edge) and finite(threshold) and edge >= threshold
            if not qualifies:
                active.discard(direction)
                continue
            if direction in active:
                continue
            active.add(direction)
            if obs["event_wall_ns"] < warmup_cutoff_ns:
                continue
            candidates.append({
                "direction": direction,
                "signal_wall_ns": obs["event_wall_ns"],
                "pair_skew_ms": obs["pair_skew_ms"],
                "initial_edge_bps": edge,
                "threshold_bps": threshold,
                "mid_premium_bps": obs["mid_premium_bps"],
                "sell_threshold_bps": obs["sell_threshold_bps"],
                "buy_threshold_bps": obs["buy_threshold_bps"],
            })
    return candidates, observations


def evaluate_candidate_horizons(
    candidates: Sequence[dict],
    observations: Sequence[dict],
    *,
    horizons_ms: Sequence[int] = HORIZONS_MS,
    tolerance_ms: float = 50.0,
) -> List[dict]:
    """Evaluate the first pair observation at or after each future horizon."""
    ordered = sorted(observations, key=lambda row: int(row["event_wall_ns"]))
    event_times = [int(row["event_wall_ns"]) for row in ordered]
    result: List[dict] = []
    for candidate in candidates:
        direction = candidate["direction"]
        initial_edge = candidate["initial_edge_bps"]
        for target_ms in horizons_ms:
            due_ns = int(candidate["signal_wall_ns"]) + int(target_ms * 1e6)
            found = next((row for row in ordered if int(row["event_wall_ns"]) >= due_ns), None)
            row = {
                "direction": direction,
                "signal_wall_ns": candidate["signal_wall_ns"],
                "target_ms": int(target_ms),
                "initial_edge_bps": initial_edge,
                "signal_threshold_bps": candidate["threshold_bps"],
                "signal_pair_skew_ms": candidate["pair_skew_ms"],
                "status": "right_censored",
                "observed_wall_ns": None,
                "sampling_lag_ms": None,
                "edge_bps": None,
                "edge_loss_bps": None,
                "edge_delta_bps": None,
                "eligible_at_horizon": None,
                "qualifies_at_horizon": None,
            }
            index = bisect.bisect_left(event_times, due_ns)
            found = ordered[index] if index < len(ordered) else None
            if found is None:
                result.append(row)
                continue
            observed_ns = int(found["event_wall_ns"])
            lag_ms = (observed_ns - due_ns) / 1e6
            row["observed_wall_ns"] = observed_ns
            row["sampling_lag_ms"] = lag_ms
            if lag_ms > tolerance_ms:
                row["status"] = "late_quote_outside_tolerance"
                result.append(row)
                continue
            if not found.get("parameter_valid"):
                row["status"] = "invalid_snapshot"
                result.append(row)
                continue
            row["status"] = "observed"
            edge_key = "sell_edge_bps" if direction == "sell_entropy" else "buy_edge_bps"
            threshold_key = "sell_threshold_bps" if direction == "sell_entropy" else "buy_threshold_bps"
            edge = found.get(edge_key)
            threshold = found.get(threshold_key)
            row["edge_bps"] = edge
            row["edge_loss_bps"] = initial_edge - edge if finite(edge) else None
            row["edge_delta_bps"] = edge - initial_edge if finite(edge) else None
            row["eligible_at_horizon"] = bool(found.get("eligible"))
            row["qualifies_at_horizon"] = bool(
                found.get("eligible") and finite(edge) and finite(threshold) and edge >= threshold
            )
            result.append(row)
    return result


def _status_counts(rows: Sequence[dict]) -> Counter:
    return Counter(str(row.get("status")) for row in rows)


def _stats_fields(values: Sequence[float], prefix: str) -> Dict[str, Any]:
    summary = numeric_summary(values)
    return {
        f"{prefix}_n": summary["n"],
        f"{prefix}_p50": summary["p50"],
        f"{prefix}_p90": summary["p90"],
        f"{prefix}_p95": summary["p95"],
        f"{prefix}_p99": summary["p99"],
        f"{prefix}_max": summary["max"],
        f"{prefix}_mean": summary["mean"],
    }


def _write_csv(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    columns: List[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=columns, extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in columns})


def _run_summary(runs: Sequence[dict]) -> List[dict]:
    output = []
    for run in runs:
        manifest = run["manifest"]
        quotes = run["records"]["quotes"]
        if quotes:
            first_ns = min(int(row["received_wall_ns"]) for row in quotes)
            last_ns = max(int(row["received_wall_ns"]) for row in quotes)
            span_sec = (last_ns - first_ns) / 1e9
        else:
            first_ns = last_ns = None
            span_sec = None
        duration_sec = (
            (int(manifest["ended_wall_ns"]) - int(manifest["started_wall_ns"])) / 1e9
            if finite(manifest.get("started_wall_ns")) and finite(manifest.get("ended_wall_ns"))
            else None
        )
        output.append({
            "run_id": run["run_id"],
            "started_utc": datetime.fromtimestamp(int(manifest["started_wall_ns"]) / 1e9, tz=timezone.utc).isoformat(),
            "ended_utc": datetime.fromtimestamp(int(manifest["ended_wall_ns"]) / 1e9, tz=timezone.utc).isoformat(),
            "run_duration_sec": duration_sec,
            "quote_rows_written": run["quote_written"],
            "quote_rows_retained": len(quotes),
            "quote_retention_pct": 100.0 * len(quotes) / run["quote_written"] if run["quote_written"] else None,
            "quote_first_utc": datetime.fromtimestamp(first_ns / 1e9, tz=timezone.utc).isoformat() if first_ns else None,
            "quote_last_utc": datetime.fromtimestamp(last_ns / 1e9, tz=timezone.utc).isoformat() if last_ns else None,
            "quote_span_sec": span_sec,
            "signal_rows_written": run["signal_written"],
            "signal_rows_retained": len(run["records"]["signals"]),
            "decay_rows_written": run["decay_written"],
            "decay_rows_retained": len(run["records"]["decay"]),
            "source_present_written": manifest.get("source_timestamp_counts", {}).get("present"),
            "source_missing_written": manifest.get("source_timestamp_counts", {}).get("missing"),
            "source_invalid_written": manifest.get("source_timestamp_counts", {}).get("invalid"),
            "dropped_updates": manifest.get("dropped_updates"),
            "dropped_lifecycle": manifest.get("dropped_lifecycle"),
            "quote_writer_errors": manifest.get("writer_stats", {}).get("quotes", {}).get("write_errors"),
        })
    return output


def _source_age_analysis(runs: Sequence[dict]) -> Tuple[List[dict], Dict[Tuple[str, str], List[float]], Dict[str, Any]]:
    buckets: Dict[str, List[float]] = defaultdict(list)
    counts: Dict[Tuple[str, str], int] = Counter()
    quality: Dict[str, Counter] = defaultdict(Counter)
    for run in runs:
        for row in run["records"]["quotes"]:
            venue = str(row.get("venue"))
            quality[venue]["total_quote_rows"] += 1
            if row.get("parameter_valid") is True:
                quality[venue]["valid_snapshot_rows"] += 1
            else:
                quality[venue]["invalid_snapshot_rows"] += 1
            status = row.get("source_timestamp_status")
            quality[venue][f"source_{status}"] += 1
            age = row.get("source_age_ms")
            if row.get("parameter_valid") is not True:
                continue
            if finite(age) and float(age) >= 0:
                buckets[venue].append(float(age))
                quality[venue]["age_nonnegative_rows"] += 1
            elif finite(age):
                quality[venue]["age_negative_rows"] += 1
            else:
                quality[venue]["age_unusable_rows"] += 1
    rows = []
    for venue in ("entropy", "lighter-rh"):
        values = buckets[venue]
        row = {"venue": venue}
        row.update(quality[venue])
        row.update(_stats_fields(values, "source_age_ms"))
        rows.append(row)
    return rows, buckets, {"quality": {key: dict(value) for key, value in quality.items()}}


def _p1_decay_analysis(runs: Sequence[dict]) -> Tuple[List[dict], List[dict], List[dict]]:
    by_age: Dict[Tuple[str, str, str, int], Dict[str, Any]] = {}
    overall: Dict[Tuple[str, int], Dict[str, Any]] = {}
    correlations: Dict[Tuple[str, str, int], Dict[str, Any]] = {}
    for run in runs:
        signal_by_id = {row["signal_id"]: row for row in run["records"]["signals"]}
        decay_by_key = {
            (row.get("signal_id"), int(row.get("target_ms", 0))): row
            for row in run["records"]["decay"]
        }
        for signal_id, signal in signal_by_id.items():
            direction = signal.get("direction")
            for venue, age_key in (
                ("entropy", "entropy_source_age_ms"),
                ("lighter-rh", "hedge_source_age_ms"),
            ):
                bucket = age_bucket(signal.get(age_key))
                for target_ms in HORIZONS_MS:
                    key = (venue, bucket, direction, target_ms)
                    item = by_age.setdefault(key, {
                        "venue": venue, "age_bucket": bucket, "direction": direction,
                        "target_ms": target_ms, "signal_count": 0,
                        "observed_count": 0, "late_count": 0,
                        "right_censored_count": 0, "invalid_snapshot_count": 0,
                        "missing_decay_count": 0, "edge_losses": [], "edge_deltas": [],
                    })
                    item["signal_count"] += 1
                    overall_item = None
                    if venue == "entropy":
                        overall_item = overall.setdefault((direction, target_ms), {
                            "direction": direction, "target_ms": target_ms,
                            "signal_count": 0, "observed_count": 0, "late_count": 0,
                            "right_censored_count": 0, "invalid_snapshot_count": 0,
                            "missing_decay_count": 0, "edge_losses": [], "edge_deltas": [],
                        })
                        overall_item["signal_count"] += 1
                    decay = decay_by_key.get((signal_id, target_ms))
                    status = decay.get("status") if decay else "missing_decay_row"
                    targets = (item, overall_item) if overall_item is not None else (item,)
                    for target in targets:
                        if status == "observed" and decay and decay.get("parameter_valid") is True:
                            target["observed_count"] += 1
                        elif status == "late_quote_outside_tolerance":
                            target["late_count"] += 1
                        elif status in {"not_observed_before_capture_end", "missing_quote_after_horizon"}:
                            target["right_censored_count"] += 1
                        elif status == "invalid_snapshot":
                            target["invalid_snapshot_count"] += 1
                        elif status == "missing_decay_row":
                            target["missing_decay_count"] += 1
                    if not decay or status != "observed" or decay.get("parameter_valid") is not True:
                        continue
                    edge_key = "sell_edge_bps" if direction == "sell_entropy" else "buy_edge_bps"
                    initial = signal.get(edge_key)
                    edge = decay.get(edge_key)
                    if finite(initial) and finite(edge):
                        loss = float(initial) - float(edge)
                        delta = -loss
                        item["edge_losses"].append(loss)
                        item["edge_deltas"].append(delta)
                        if overall_item is not None:
                            overall_item["edge_losses"].append(loss)
                            overall_item["edge_deltas"].append(delta)
                        age = signal.get(age_key)
                        if finite(age) and float(age) >= 0:
                            corr_key = (venue, direction, target_ms)
                            corr = correlations.setdefault(corr_key, {
                                "venue": venue, "direction": direction,
                                "target_ms": target_ms, "ages": [], "losses": [],
                            })
                            corr["ages"].append(float(age))
                            corr["losses"].append(loss)

    age_rows = []
    for item in by_age.values():
        denominator = item["signal_count"]
        row = {key: value for key, value in item.items() if key not in {"edge_losses", "edge_deltas"}}
        row["observable_pct"] = 100.0 * item["observed_count"] / denominator if denominator else None
        row["unobservable_pct"] = 100.0 * (
            item["late_count"] + item["right_censored_count"] + item["invalid_snapshot_count"] + item["missing_decay_count"]
        ) / denominator if denominator else None
        row.update(_stats_fields(item["edge_losses"], "edge_loss_bps"))
        row.update(_stats_fields(item["edge_deltas"], "edge_delta_bps"))
        age_rows.append(row)

    overall_rows = []
    for item in overall.values():
        denominator = item["signal_count"]
        row = {key: value for key, value in item.items() if key not in {"edge_losses", "edge_deltas"}}
        row["observable_pct"] = 100.0 * item["observed_count"] / denominator if denominator else None
        row["unobservable_pct"] = 100.0 * (
            item["late_count"] + item["right_censored_count"] + item["invalid_snapshot_count"] + item["missing_decay_count"]
        ) / denominator if denominator else None
        row.update(_stats_fields(item["edge_losses"], "edge_loss_bps"))
        row.update(_stats_fields(item["edge_deltas"], "edge_delta_bps"))
        overall_rows.append(row)

    correlation_rows = []
    for item in correlations.values():
        pearson, spearman = correlation(item["ages"], item["losses"])
        row = {
            "venue": item["venue"], "direction": item["direction"],
            "target_ms": item["target_ms"], "n": len(item["ages"]),
            "pearson_age_vs_edge_loss": pearson,
            "spearman_age_vs_edge_loss": spearman,
        }
        correlation_rows.append(row)
    return sorted(age_rows, key=lambda row: (row["venue"], row["age_bucket"], row["direction"], row["target_ms"])), sorted(overall_rows, key=lambda row: (row["direction"], row["target_ms"])), sorted(correlation_rows, key=lambda row: (row["venue"], row["direction"], row["target_ms"]))


def _replay_analysis(runs: Sequence[dict]) -> Tuple[Dict[float, Dict[str, Any]], List[dict], List[dict], List[dict]]:
    replay_by_threshold: Dict[float, Dict[str, Any]] = {}
    run_rows = _run_summary(runs)
    for threshold in SKEW_THRESHOLDS_MS:
        replay_by_threshold[threshold] = {"candidates": [], "evaluations": [], "observations": [], "by_run": {}}

    for run, run_row in zip(runs, run_rows):
        quotes = run["records"]["quotes"]
        for threshold in SKEW_THRESHOLDS_MS:
            candidates, observations = replay_candidates(quotes, skew_threshold_ms=threshold)
            evaluations = evaluate_candidate_horizons(candidates, observations)
            for item in candidates:
                item = dict(item)
                item["run_id"] = run["run_id"]
                replay_by_threshold[threshold]["candidates"].append(item)
            for item in evaluations:
                item = dict(item)
                item["run_id"] = run["run_id"]
                replay_by_threshold[threshold]["evaluations"].append(item)
            replay_by_threshold[threshold]["observations"].extend(observations)
            replay_by_threshold[threshold]["by_run"][run["run_id"]] = {
                "candidates": candidates, "evaluations": evaluations,
                "observations": observations,
            }
            run_row[f"replay_candidates_{int(threshold)}ms"] = len(candidates)

        actual = run["records"]["signals"]
        segment_start = int(quotes[0]["received_wall_ns"]) + 1_000_000_000 if quotes else 0
        actual_segment = {
            (int(row["signal_wall_ns"]), row.get("direction"))
            for row in actual if int(row.get("signal_wall_ns", 0)) >= segment_start
        }
        replay_250 = replay_by_threshold[250.0]["by_run"][run["run_id"]]["candidates"]
        replay_set = {(int(row["signal_wall_ns"]), row.get("direction")) for row in replay_250}
        run_row["actual_250_signals_in_retained_segment"] = len(actual_segment)
        run_row["replay_250_exact_matches"] = len(actual_segment & replay_set)
        run_row["replay_250_extra"] = len(replay_set - actual_segment)
        run_row["replay_250_missing"] = len(actual_segment - replay_set)

    baseline_250 = len(replay_by_threshold[250.0]["candidates"])
    p2_summary = []
    p2_decay = []
    for threshold in SKEW_THRESHOLDS_MS:
        candidates = replay_by_threshold[threshold]["candidates"]
        for direction in ("sell_entropy", "buy_entropy", "all"):
            selected = [row for row in candidates if direction == "all" or row["direction"] == direction]
            baseline_selected = [
                row for row in replay_by_threshold[250.0]["candidates"]
                if direction == "all" or row["direction"] == direction
            ]
            gaps: List[float] = []
            grouped: Dict[Tuple[str, str], List[int]] = defaultdict(list)
            for row in selected:
                grouped[(row["run_id"], row["direction"])].append(int(row["signal_wall_ns"]))
            for times in grouped.values():
                times.sort()
                gaps.extend((b - a) / 1e6 for a, b in zip(times, times[1:]))
            edge_values = [row["initial_edge_bps"] for row in selected if finite(row.get("initial_edge_bps"))]
            p2_summary.append({
                "skew_threshold_ms": threshold, "direction": direction,
                "candidate_count": len(selected),
                "baseline_250_candidate_count": len(baseline_selected),
                "candidate_retention_vs_250_pct": 100.0 * len(selected) / len(baseline_selected) if baseline_selected else None,
                "candidate_edge_p50_bps": percentile(edge_values, 0.5),
                "candidate_edge_p90_bps": percentile(edge_values, 0.9),
                "interarrival_p50_ms": percentile(gaps, 0.5),
                "interarrival_p90_ms": percentile(gaps, 0.9),
                "interarrival_n": len(gaps),
                "replay_250_exact_match_rate_pct": (
                    100.0 * sum(row["replay_250_exact_matches"] for row in run_rows) /
                    sum(row["actual_250_signals_in_retained_segment"] for row in run_rows)
                    if sum(row["actual_250_signals_in_retained_segment"] for row in run_rows) else None
                ),
            })
        evaluations = replay_by_threshold[threshold]["evaluations"]
        for direction in ("sell_entropy", "buy_entropy", "all"):
            selected = [row for row in evaluations if direction == "all" or row["direction"] == direction]
            for target_ms in HORIZONS_MS:
                horizon = [row for row in selected if row["target_ms"] == target_ms]
                observed = [row for row in horizon if row["status"] == "observed" and finite(row.get("edge_bps"))]
                status = _status_counts(horizon)
                qualifying = [row for row in observed if row.get("qualifies_at_horizon")]
                eligible = [row for row in observed if row.get("eligible_at_horizon")]
                row = {
                    "skew_threshold_ms": threshold, "direction": direction,
                    "target_ms": target_ms, "candidate_count": len(horizon),
                    "observed_count": len(observed),
                    "late_count": status.get("late_quote_outside_tolerance", 0),
                    "invalid_snapshot_count": status.get("invalid_snapshot", 0),
                    "right_censored_count": status.get("right_censored", 0),
                    "eligible_at_horizon_count": len(eligible),
                    "qualifying_at_horizon_count": len(qualifying),
                    "observable_pct": 100.0 * len(observed) / len(horizon) if horizon else None,
                    "eligible_at_horizon_pct": 100.0 * len(eligible) / len(observed) if observed else None,
                    "qualifying_pct_of_candidates": 100.0 * len(qualifying) / len(horizon) if horizon else None,
                }
                row.update(_stats_fields([item["edge_loss_bps"] for item in observed if finite(item.get("edge_loss_bps"))], "edge_loss_bps"))
                p2_decay.append(row)
    return replay_by_threshold, run_rows, p2_summary, p2_decay


def _p3_analysis(replay_by_threshold: Dict[float, Dict[str, Any]]) -> List[dict]:
    base_candidates = replay_by_threshold[250.0]["candidates"]
    base_evaluations = replay_by_threshold[250.0]["evaluations"]
    rows = []
    for persistence_ms in PERSISTENCE_THRESHOLDS_MS:
        for direction in ("sell_entropy", "buy_entropy", "all"):
            candidates = [row for row in base_candidates if direction == "all" or row["direction"] == direction]
            evaluations = [
                row for row in base_evaluations
                if row["target_ms"] == int(persistence_ms) and (direction == "all" or row["direction"] == direction)
            ]
            observed = [row for row in evaluations if row["status"] == "observed" and finite(row.get("edge_bps"))]
            status = _status_counts(evaluations)
            qualifying = [row for row in observed if row.get("qualifies_at_horizon")]
            edge_losses = [row["edge_loss_bps"] for row in observed if finite(row.get("edge_loss_bps"))]
            rows.append({
                "base_skew_threshold_ms": 250.0,
                "persistence_threshold_ms": persistence_ms,
                "direction": direction,
                "candidate_count": len(candidates),
                "timely_observed_count": len(observed),
                "qualifying_at_persistence_count": len(qualifying),
                "candidate_retention_pct": 100.0 * len(qualifying) / len(candidates) if candidates else None,
                "retention_among_timely_observed_pct": 100.0 * len(qualifying) / len(observed) if observed else None,
                "late_count": status.get("late_quote_outside_tolerance", 0),
                "invalid_snapshot_count": status.get("invalid_snapshot", 0),
                "right_censored_count": status.get("right_censored", 0),
                "timely_observable_pct": 100.0 * len(observed) / len(candidates) if candidates else None,
                "edge_loss_p50_bps": percentile(edge_losses, 0.5),
                "edge_loss_p90_bps": percentile(edge_losses, 0.9),
                "waiting_cost_n": len(edge_losses),
            })
    return rows


def _time_segments(runs: Sequence[dict]) -> List[dict]:
    rows = []
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for run in runs:
        for signal in run["records"]["signals"]:
            segment = utc_hour_label(int(signal["signal_wall_ns"]))
            direction = signal.get("direction")
            item = grouped.setdefault((segment, direction), {
                "segment_utc": segment, "direction": direction,
                "signal_count": 0, "source_age_entropy": [], "source_age_hedge": [],
                "edge_at_signal": [],
            })
            item["signal_count"] += 1
            if finite(signal.get("entropy_source_age_ms")):
                item["source_age_entropy"].append(float(signal["entropy_source_age_ms"]))
            if finite(signal.get("hedge_source_age_ms")):
                item["source_age_hedge"].append(float(signal["hedge_source_age_ms"]))
            edge_key = "sell_edge_bps" if direction == "sell_entropy" else "buy_edge_bps"
            if finite(signal.get(edge_key)):
                item["edge_at_signal"].append(float(signal[edge_key]))
    for item in grouped.values():
        row = {key: value for key, value in item.items() if not isinstance(value, list)}
        row.update(_stats_fields(item["source_age_entropy"], "entropy_source_age_ms"))
        row.update(_stats_fields(item["source_age_hedge"], "hedge_source_age_ms"))
        row.update(_stats_fields(item["edge_at_signal"], "signal_edge_bps"))
        rows.append(row)
    return sorted(rows, key=lambda row: (row["segment_utc"], row["direction"]))


def _make_plots(output_dir: Path, age_rows: Sequence[dict], p2_summary: Sequence[dict], p3_rows: Sequence[dict]) -> List[str]:
    try:
        os.environ.setdefault("MPLCONFIGDIR", str(output_dir / ".mplconfig"))
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    labels = [label for label, _, _ in AGE_BUCKETS]
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharex=True)
    for row_index, venue in enumerate(("entropy", "lighter-rh")):
        for col_index, target in enumerate((100, 500)):
            ax = axes[row_index][col_index]
            for direction, color in (("sell_entropy", "tab:red"), ("buy_entropy", "tab:blue")):
                values = []
                for label in labels:
                    found = next((r for r in age_rows if r["venue"] == venue and r["age_bucket"] == label and r["direction"] == direction and r["target_ms"] == target), None)
                    values.append(found.get("edge_loss_bps_p50") if found else None)
                ax.plot(labels, [float(v) if finite(v) else math.nan for v in values], marker="o", label=direction, color=color)
            ax.axhline(0, color="black", linewidth=0.7)
            ax.set_title(f"{venue} source-age bucket, {target} ms")
            ax.set_ylabel("edge loss p50 (bps)")
            ax.tick_params(axis="x", rotation=35)
            ax.grid(alpha=0.25)
            if row_index == 0 and col_index == 1:
                ax.legend(fontsize=8)
    fig.suptitle("P1 source age and observed edge decay")
    fig.tight_layout()
    path = output_dir / "p1_source_age_edge_decay.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    paths.append(path.name)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    for direction, color in (("sell_entropy", "tab:red"), ("buy_entropy", "tab:blue"), ("all", "tab:gray")):
        rows = [r for r in p2_summary if r["direction"] == direction]
        axes[0].plot([r["skew_threshold_ms"] for r in rows], [r["candidate_count"] for r in rows], marker="o", label=direction, color=color)
    axes[0].set_title("P2 replay candidates by skew")
    axes[0].set_xlabel("skew threshold (ms)")
    axes[0].set_ylabel("candidate events")
    axes[0].grid(alpha=0.25)
    axes[0].legend(fontsize=8)
    rows = [r for r in p3_rows if r["direction"] == "all"]
    axes[1].plot([r["persistence_threshold_ms"] for r in rows], [r["candidate_retention_pct"] for r in rows], marker="o", color="tab:green")
    axes[1].set_title("P3 retained at persistence horizon")
    axes[1].set_xlabel("persistence threshold (ms)")
    axes[1].set_ylabel("retained candidates (%)")
    axes[1].grid(alpha=0.25)
    fig.tight_layout()
    path = output_dir / "p2_p3_thresholds.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    paths.append(path.name)
    return paths


def _report(
    path: Path,
    selection: dict,
    run_rows: Sequence[dict],
    age_rows: Sequence[dict],
    p1_overall: Sequence[dict],
    correlations: Sequence[dict],
    p2_summary: Sequence[dict],
    p2_decay: Sequence[dict],
    p3_rows: Sequence[dict],
    time_rows: Sequence[dict],
    plot_names: Sequence[str],
) -> None:
    total_written = sum(int(row["quote_rows_written"] or 0) for row in run_rows)
    total_retained = sum(int(row["quote_rows_retained"] or 0) for row in run_rows)
    total_signals = sum(int(row["signal_rows_retained"] or 0) for row in run_rows)
    total_decay = sum(int(row["decay_rows_retained"] or 0) for row in run_rows)
    match_count = sum(int(row["replay_250_exact_matches"]) for row in run_rows)
    actual_segment = sum(int(row["actual_250_signals_in_retained_segment"]) for row in run_rows)
    by_key = {(row["direction"], row["target_ms"]): row for row in p1_overall}
    age_text = []
    for row in age_rows:
        age_text.append(
            f'| {row["venue"]} | {row.get("source_age_ms_p50")} | {row.get("source_age_ms_p90")} | '
            f'{row.get("source_age_ms_p95")} | {row.get("source_age_ms_p99")} | {row.get("source_age_ms_max")} | '
            f'{row.get("age_nonnegative_rows", 0)} | {row.get("invalid_snapshot_rows", 0)} | '
            f'{row.get("source_missing", 0)} | {row.get("source_invalid", 0)} |'
        )
    p1_text = []
    for key in (("sell_entropy", 50), ("sell_entropy", 250), ("sell_entropy", 1000),
                ("buy_entropy", 50), ("buy_entropy", 250), ("buy_entropy", 1000)):
        row = by_key.get(key, {})
        p1_text.append(
            f'| {key[0]} | {key[1]} | {row.get("signal_count", 0)} | {row.get("observed_count", 0)} | '
            f'{row.get("observable_pct")} | {row.get("edge_loss_bps_p50")} | {row.get("edge_loss_bps_p90")} | '
            f'{row.get("late_count", 0)} | {row.get("right_censored_count", 0)} |'
        )
    correlation_lookup = {
        (row["venue"], row["direction"], row["target_ms"]): row
        for row in correlations
    }
    p1_corr_text = []
    for venue in ("entropy", "lighter-rh"):
        for direction in ("sell_entropy", "buy_entropy"):
            row = correlation_lookup.get((venue, direction, 1000), {})
            p1_corr_text.append(
                f'| {venue} | {direction} | {row.get("n", 0)} | '
                f'{row.get("spearman_age_vs_edge_loss")} | {row.get("pearson_age_vs_edge_loss")} |'
            )
    p2_all = [row for row in p2_summary if row["direction"] == "all"]
    p3_all = [row for row in p3_rows if row["direction"] == "all"]
    p2_text = [
        f'| {row["skew_threshold_ms"]:.0f} | {row["candidate_count"]} | {row["candidate_retention_vs_250_pct"]} | '
        f'{row["candidate_edge_p50_bps"]} | {row["interarrival_p50_ms"]} |'
        for row in p2_all
    ]
    p2_decay_lookup = {
        (row["skew_threshold_ms"], row["target_ms"]): row
        for row in p2_decay
        if row["direction"] == "all"
    }
    p2_decay_text = []
    for target_ms in (500, 1000):
        values = []
        for skew_ms in SKEW_THRESHOLDS_MS:
            row = p2_decay_lookup.get((skew_ms, target_ms), {})
            values.append(
                f'{skew_ms:.0f} ms: p90 loss {row.get("edge_loss_bps_p90")}, '
                f'qualifying {row.get("qualifying_pct_of_candidates")}%'
            )
        p2_decay_text.append(f'- {target_ms} ms horizon: ' + '; '.join(values) + '.')
    p3_text = [
        f'| {row["persistence_threshold_ms"]:.0f} | {row["candidate_count"]} | {row["timely_observed_count"]} | '
        f'{row["qualifying_at_persistence_count"]} | {row["candidate_retention_pct"]} | '
        f'{row["edge_loss_p50_bps"]} | {row["edge_loss_p90_bps"]} | {row["right_censored_count"]} |'
        for row in p3_all
    ]
    plot_text = "\n".join(f"- {name}" for name in plot_names) if plot_names else "- matplotlib unavailable; no chart files generated"
    report = f"""# SNDK × Lighter RH Phase 3 分段研究

本研究只讀取 {selection["completed_runs"]} 個 checksum 通過的 completed runs，排除 active run、不完整 run 與 checksum 不一致檔案。沒有讀取或修改 Engine、交易設定、Recorder 或帳戶資料。所有 Edge 是行情觀測值，不是成交或 PnL。

## 結論摘要

- P1：Entropy source age 明顯高於 RH。這是同步／接收品質差異的觀察，不等同於單向網路延遲因果。
- P2：250 ms replay 與實際 signals 在保留 quote segment 內逐筆相符率為 {100.0 * match_count / actual_segment if actual_segment else None:.2f}%（{match_count}/{actual_segment}）。100／500 ms 結果是 retained quote segment 的反事實 replay，不是完整 run 的無偏事件數。
- P3：Persistence 結果只檢查訊號後第一個不早於指定 horizon 的 pair observation；它不是連續每毫秒 persistence，也沒有把未來資料帶入訊號產生。
- 不存在足夠的完整、未淘汰 quote 來把 500 ms skew 或 persistence 結果提升為實盤可執行性證據。Run02 建議維持現有 250 ms skew 與原策略參數；若要做一項額外測試，優先做獨立 read-only 的 250 ms persistence 觀測，不接入 Engine。
## 資料範圍與保留率

- Completed runs: {selection["completed_runs"]}; excluded runs: {len(selection.get("excluded_runs", []))}.
- Quotes retained: {total_retained:,} / {total_written:,} written ({100.0 * total_retained / total_written if total_written else None:.2f}% weighted). Retention is per writer and varies by run; it is not assumed equal to signals or decay.
- Signals retained: {total_signals:,}. Decay rows retained: {total_decay:,}; expected 5 horizons per signal is {total_signals * 5:,}.
- P2 replay quote coverage is limited to each run's retained time segment. Replay never joins adjacent runs.
- The 45 completed-run manifests have no recorded source timestamp missing/invalid rows. Quote age distributions below still use only rows with parameter_valid=true and non-negative finite age.
- Right-censored and late observations remain separate. Missing decay rows and invalid snapshots are not converted to zero.
## P1 — Source Age × Edge Decay

### Source age summary

| venue | P50 ms | P90 ms | P95 ms | P99 ms | max ms | usable rows | invalid Rolling rows | source missing | source invalid |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(age_text)}

The source age is received_wall_ns minus the explicit source timestamp. It is not validated one-way network latency. The age-to-edge comparison is correlational and is affected by market state, feed scheduling, pair skew, and recorder retention.

### Actual 250 ms candidate decay

| direction | horizon ms | signal count | timely observed | observable % | edge loss p50 bps | edge loss p90 bps | late | right-censored |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(p1_text)}

Positive edge loss means the later executable edge is lower than the signal edge. Late rows are outside the recorder's 50 ms sampling tolerance and have no edge value. Correlation rows are in p1_age_correlations.csv; a positive correlation means older source age is associated with larger edge loss, not that age caused the loss.

At the 1000 ms horizon, Spearman age versus edge-loss correlations are:

| venue | direction | n | Spearman | Pearson |
|---|---|---:|---:|---:|
{chr(10).join(p1_corr_text)}

The correlations are weak to modest and are not consistent at the shorter horizons. Entropy sell is the strongest positive 1000 ms association in this sample; RH associations are near zero. The result does not support a causal source-age filter.

## P2 — Pair Skew

| skew threshold ms | candidate events | retention vs 250 % | candidate edge P50 bps | same-direction interarrival P50 ms |
|---:|---:|---:|---:|---:|
{chr(10).join(p2_text)}

The 500 ms case cannot recover events that were never recorded after the original 250 ms gate. The replay uses retained quotes only. The 100 ms case can also change the active latch and therefore is not a simple row filter. The 250 ms exact-match check is a replay integrity check, not proof that retained quotes are complete.

Selected post-filter decay comparisons:

{chr(10).join(p2_decay_text)}

Detailed post-signal decay for every skew threshold and horizon is in p2_skew_decay.csv.

## P3 — Persistence

| persistence horizon ms | candidate events | timely observed | qualifying at horizon | retained % | edge loss P50 bps | edge loss P90 bps | right-censored |
|---:|---:|---:|---:|---:|---:|---:|---:|
{chr(10).join(p3_text)}

Qualifying at persistence horizon means the first timely observation at or after the horizon still had a valid Rolling snapshot, passed the selected 250 ms skew gate, and met the direction threshold. This is a forward-looking offline label used to evaluate waiting cost. It is not a live decision implementation and does not prove continuous persistence between observations.

## Per-run and time segments

- Per-run coverage and candidate replay counts: run_summary.csv.
- Four-hour UTC segment counts and age/edge summaries: time_segment_summary.csv.
- The replay is reset at every run boundary. The first 1 second of each retained quote segment is used as warm-up and excluded from candidate counts to avoid treating an unknown pre-segment latch state as a new signal.

## Evidence strength and limitations

- P1 source-age percentiles are descriptive and have large retained-row coverage, but quote retention is approximately half of written quotes overall and varies by run. No causal latency claim is justified.
- P1 actual signal/decay files are complete for the selected runs, but late and right-censored horizons reduce usable edge samples.
- P2/P3 are strongest as implementation-consistency and directional sensitivity checks within the retained quote segments. They are not full-run event-rate estimates because the running collector used max_files=2 and evicted earlier quote files.
- No result is a fill, realized capture, funding result, or account PnL. There is no basis here to optimize a production trading threshold.

## Run02 recommendation

Maintain the existing 250 ms pair-skew setting and all formal strategy parameters. The single additional experiment worth running is a separate read-only 250 ms persistence observation at the existing candidate threshold, with complete quote retention and no Engine integration. Do not treat this recommendation as authorization to change or start trading.

## Verification

- New offline analyzer tests: 2 passed.
- Quote-latency focused suite including the analyzer: 25 passed.
- Full pytest: 121 passed and 1 known baseline Rich Dashboard width/truncation failure, also present on the baseline branch.
- compileall and git diff --check: passed.
- No Engine, Rolling, Inventory Ladder, IOC, Recorder, or formal strategy files were changed.

## Artifacts

- statistics.json contains machine-readable selection, data-quality, P1, P2 and P3 results.
- source_age_summary.csv
- p1_edge_decay_by_age.csv
- p1_edge_decay_overall.csv
- p1_age_correlations.csv
- p2_skew_summary.csv
- p2_skew_decay.csv
- p3_persistence_summary.csv
- run_summary.csv
- time_segment_summary.csv
{plot_text}
"""
    path.write_text(report, encoding="utf-8")


def analyze(base_dir: str | Path, output_dir: str | Path) -> Dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    runs, selection = load_completed_runs(base_dir)
    run_rows = _run_summary(runs)
    age_rows, age_values, age_quality = _source_age_analysis(runs)
    p1_age_rows, p1_overall, p1_correlations = _p1_decay_analysis(runs)
    replay_by_threshold, run_rows, p2_summary, p2_decay = _replay_analysis(runs)
    p3_rows = _p3_analysis(replay_by_threshold)
    time_rows = _time_segments(runs)
    plots = _make_plots(output, p1_age_rows, p2_summary, p3_rows)

    quality = {
        "quote_rows_written": sum(run["quote_written"] for run in runs),
        "quote_rows_retained": sum(len(run["records"]["quotes"]) for run in runs),
        "signal_rows_written": sum(run["signal_written"] for run in runs),
        "signal_rows_retained": sum(len(run["records"]["signals"]) for run in runs),
        "decay_rows_written": sum(run["decay_written"] for run in runs),
        "decay_rows_retained": sum(len(run["records"]["decay"]) for run in runs),
        "quote_retention_weighted_pct": (
            100.0 * sum(len(run["records"]["quotes"]) for run in runs) /
            sum(run["quote_written"] for run in runs)
            if sum(run["quote_written"] for run in runs) else None
        ),
        "quote_retention_mean_per_run_pct": (
            statistics.fmean(
                100.0 * len(run["records"]["quotes"]) / run["quote_written"]
                for run in runs if run["quote_written"]
            ) if any(run["quote_written"] for run in runs) else None
        ),
    }
    result = {
        "analysis_schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_dir": str(Path(base_dir)),
        "output_dir": str(output),
        "selection": selection,
        "data_quality": quality,
        "source_age_summary": age_rows,
        "p1_edge_decay_by_age": p1_age_rows,
        "p1_edge_decay_overall": p1_overall,
        "p1_age_correlations": p1_correlations,
        "p2_skew_summary": p2_summary,
        "p2_skew_decay": p2_decay,
        "p3_persistence_summary": p3_rows,
        "run_summary": run_rows,
        "time_segment_summary": time_rows,
        "plots": plots,
        "method": {
            "age_buckets_ms": ["0-50", "50-100", "100-250", "250-500", "500+"],
            "decay_horizons_ms": list(HORIZONS_MS),
            "skew_thresholds_ms": list(SKEW_THRESHOLDS_MS),
            "persistence_thresholds_ms": list(PERSISTENCE_THRESHOLDS_MS),
            "observation_tolerance_ms": 50.0,
            "replay_warmup_ms": 1000.0,
            "rolling_invalid_policy": "exclude invalid snapshots and never fallback to Fixed",
            "cross_run_policy": "reset replay and never join observations across run boundaries",
            "edge_definition": "sell=(entropy_bid/lighter_ask-1)*1e4; buy=(lighter_bid/entropy_ask-1)*1e4",
            "edge_loss_definition": "signal_edge_bps - first timely future edge_bps",
            "persistence_definition": "first timely observation at or after horizon still qualifies; not continuous persistence",
        },
    }
    (output / "statistics.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    _write_csv(output / "source_age_summary.csv", age_rows)
    _write_csv(output / "p1_edge_decay_by_age.csv", p1_age_rows)
    _write_csv(output / "p1_edge_decay_overall.csv", p1_overall)
    _write_csv(output / "p1_age_correlations.csv", p1_correlations)
    _write_csv(output / "p2_skew_summary.csv", p2_summary)
    _write_csv(output / "p2_skew_decay.csv", p2_decay)
    _write_csv(output / "p3_persistence_summary.csv", p3_rows)
    _write_csv(output / "run_summary.csv", run_rows)
    _write_csv(output / "time_segment_summary.csv", time_rows)
    _report(
        Path(output).parent / "sndk_rh_phase3_segment_analysis.md",
        selection, run_rows, age_rows, p1_overall, p1_correlations,
        p2_summary, p2_decay, p3_rows, time_rows, plots,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="analyze retained SNDK RH Phase 3 runs")
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    result = analyze(args.backup_dir, args.output_dir)
    print(json.dumps({
        "completed_runs": result["selection"]["completed_runs"],
        "quote_rows_retained": result["data_quality"]["quote_rows_retained"],
        "signal_rows_retained": result["data_quality"]["signal_rows_retained"],
        "decay_rows_retained": result["data_quality"]["decay_rows_retained"],
        "output_dir": result["output_dir"],
        "report": str(Path(result["output_dir"]).parent / "sndk_rh_phase3_segment_analysis.md"),
        "plots": result["plots"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
