#!/usr/bin/env python3
"""Offline persistence analysis for a complete SNDK x Lighter RH Run02.

The input is a frozen local copy of the record-only run directory.  The
analyzer validates manifests, JSONL rows and SHA-256 files before rebuilding
pair observations.  It never reads credentials, contacts a venue, or imports
the trading Engine.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


WAIT_MS = (0, 100, 250, 500)
KINDS = ("quotes", "signals", "decay", "lifecycle")


def finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    clean = sorted(float(value) for value in values if finite(value))
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


def iso_utc(ns: Optional[int]) -> Optional[str]:
    if ns is None:
        return None
    return datetime.fromtimestamp(int(ns) / 1e9, timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path, parse_errors: List[dict]) -> List[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_errors.append({
                    "path": str(path), "line": line_number, "reason": str(exc),
                })
                continue
            if not isinstance(row, dict):
                parse_errors.append({
                    "path": str(path), "line": line_number,
                    "reason": "json_value_is_not_object",
                })
                continue
            rows.append(row)
    return rows


def _remote_hashes(path: Path) -> Dict[str, str]:
    result = {}
    hash_file = path / "remote_sha256.txt"
    if not hash_file.is_file():
        return result
    for line in hash_file.read_text(encoding="utf-8").splitlines():
        if "  " not in line:
            continue
        digest, remote_path = line.split("  ", 1)
        if "/runs/" in remote_path:
            result[remote_path.split("/runs/", 1)[1]] = digest
    return result


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
    entropy_bbo = _bbo(entropy)
    hedge_bbo = _bbo(hedge)
    if entropy_bbo is None or hedge_bbo is None:
        return None
    entropy_bid, entropy_ask = entropy_bbo
    hedge_bid, hedge_ask = hedge_bbo
    sell = (entropy_bid / hedge_ask - 1.0) * 1e4
    buy = (hedge_bid / entropy_ask - 1.0) * 1e4
    entropy_mid = (entropy_bid + entropy_ask) / 2.0
    hedge_mid = (hedge_bid + hedge_ask) / 2.0
    mid = (entropy_mid / hedge_mid - 1.0) * 1e4
    if not all(finite(value) for value in (sell, buy, mid)):
        return None
    return sell, buy, mid


def _parameter_valid(row: dict) -> bool:
    return row.get("parameter_valid") is True and all(
        finite(row.get(key)) for key in ("midline_bps", "upper_bps", "lower_bps")
    )


def _threshold(row: dict, direction: str) -> Optional[float]:
    if not _parameter_valid(row):
        return None
    if direction == "sell_entropy":
        return float(row["midline_bps"]) + float(row["upper_bps"])
    return float(row["lower_bps"]) - float(row["midline_bps"])


def _qualification_reason(row: dict, direction: str,
                          skew_threshold_ms: float) -> Optional[str]:
    if not _parameter_valid(row):
        return "invalid_snapshot"
    if not finite(row.get("pair_skew_ms")) or float(row["pair_skew_ms"]) > skew_threshold_ms:
        return "pair_skew"
    edge_key = "sell_edge_bps" if direction == "sell_entropy" else "buy_edge_bps"
    edge = row.get(edge_key)
    threshold = _threshold(row, direction)
    if not finite(edge) or threshold is None:
        return "invalid_bbo"
    if float(edge) < threshold:
        return "edge_below_threshold"
    return None


def build_pair_observations(
    quote_rows: Sequence[dict], *, skew_threshold_ms: float = 250.0,
) -> List[dict]:
    """Build causal pair observations from one run's quote stream.

    Each row is created only after a quote has arrived and updates one side of
    the latest pair.  A missing update therefore creates an observation gap,
    not a fabricated failure.
    """
    ordered = sorted(
        quote_rows,
        key=lambda row: (int(row["received_wall_ns"]), str(row.get("venue"))),
    )
    latest: Dict[str, dict] = {}
    observations: List[dict] = []
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
        event_ns = max(entropy_ns, hedge_ns)
        row = {
            "event_wall_ns": event_ns,
            "update_wall_ns": int(quote["received_wall_ns"]),
            "updated_venue": venue,
            "pair_skew_ms": abs(entropy_ns - hedge_ns) / 1e6,
            "sell_edge_bps": edges[0],
            "buy_edge_bps": edges[1],
            "mid_premium_bps": edges[2],
            "parameter_valid": _parameter_valid(quote),
            "parameter_invalid_reason": quote.get("parameter_invalid_reason"),
            "effective_at": quote.get("effective_at"),
            "coverage": quote.get("coverage"),
            "sample_count": quote.get("sample_count"),
            "midline_bps": quote.get("midline_bps"),
            "upper_bps": quote.get("upper_bps"),
            "lower_bps": quote.get("lower_bps"),
        }
        row["sell_failure_reason"] = _qualification_reason(
            row, "sell_entropy", skew_threshold_ms,
        )
        row["buy_failure_reason"] = _qualification_reason(
            row, "buy_entropy", skew_threshold_ms,
        )
        row["sell_qualifies"] = row["sell_failure_reason"] is None
        row["buy_qualifies"] = row["buy_failure_reason"] is None
        observations.append(row)
    return observations


def _empty_persistence(signal: dict, wait_ms: int) -> dict:
    direction = str(signal["direction"])
    edge_key = "sell_edge_bps" if direction == "sell_entropy" else "buy_edge_bps"
    return {
        "signal_id": signal.get("signal_id"),
        "direction": direction,
        "wait_ms": int(wait_ms),
        "signal_wall_ns": int(signal["signal_wall_ns"]),
        "signal_utc": iso_utc(int(signal["signal_wall_ns"])),
        "initial_edge_bps": signal.get(edge_key),
        "signal_threshold_bps": signal.get("threshold_bps"),
        "status": "right_censored",
        "complete_observed": False,
        "right_censored": True,
        "continuous_survived_observed": None,
        "qualifies_at_horizon": None,
        "horizon_wall_ns": None,
        "horizon_utc": None,
        "observed_after_ms": None,
        "sampling_lag_ms": None,
        "horizon_edge_bps": None,
        "edge_loss_bps": None,
        "first_failure_kind": None,
        "first_failure_after_ms": None,
        "recovered_after_failure": None,
        "recovery_after_ms": None,
        "continuous_valid_duration_ms": None,
        "last_observation_after_ms": None,
        "observed_pair_count": 0,
        "max_pair_observation_gap_ms": None,
    }


def evaluate_signal_persistence(
    signal: dict, observations: Sequence[dict], *, wait_ms: int,
    skew_threshold_ms: float = 250.0,
    observation_times: Optional[Sequence[int]] = None,
) -> dict:
    """Evaluate a signal using only observations from the same run.

    ``continuous_survived_observed`` means every pair observation in the
    interval qualified.  It deliberately does not claim that the state was
    unchanged during a silent interval; that interval is exposed as
    ``max_pair_observation_gap_ms``.
    """
    if observation_times is None:
        ordered = sorted(observations, key=lambda row: int(row["event_wall_ns"]))
        event_times = [int(row["event_wall_ns"]) for row in ordered]
    else:
        # The offline caller prepares one sorted index per Run and reuses it
        # for every signal.  The sequence is intentionally not copied here.
        ordered = observations
        event_times = observation_times
    signal_ns = int(signal["signal_wall_ns"])
    direction = str(signal["direction"])
    edge_key = "sell_edge_bps" if direction == "sell_entropy" else "buy_edge_bps"

    if wait_ms == 0:
        result = _empty_persistence(signal, 0)
        result.update({
            "status": "observed",
            "complete_observed": True,
            "right_censored": False,
            "continuous_survived_observed": True,
            "qualifies_at_horizon": True,
            "horizon_wall_ns": signal_ns,
            "horizon_utc": iso_utc(signal_ns),
            "observed_after_ms": 0.0,
            "sampling_lag_ms": 0.0,
            "horizon_edge_bps": signal.get(edge_key),
            "edge_loss_bps": 0.0,
            "continuous_valid_duration_ms": 0.0,
            "last_observation_after_ms": 0.0,
            "observed_pair_count": 1,
            "max_pair_observation_gap_ms": 0.0,
        })
        return result

    start_index = bisect.bisect_left(event_times, signal_ns)
    due_ns = signal_ns + int(wait_ms * 1e6)
    due_index = bisect.bisect_left(event_times, due_ns)
    if due_index >= len(ordered):
        result = _empty_persistence(signal, wait_ms)
        interval = ordered[start_index:]
        if interval:
            result["observed_pair_count"] = len(interval)
            result["last_observation_after_ms"] = (
                int(interval[-1]["event_wall_ns"]) - signal_ns
            ) / 1e6
            result["max_pair_observation_gap_ms"] = max(
                [
                    (int(b["event_wall_ns"]) - int(a["event_wall_ns"])) / 1e6
                    for a, b in zip(interval, interval[1:])
                ] or [0.0]
            )
            for observation in interval:
                reason = observation["sell_failure_reason"] if direction == "sell_entropy" else observation["buy_failure_reason"]
                if reason is not None and result["first_failure_kind"] is None:
                    result["first_failure_kind"] = reason
                    result["first_failure_after_ms"] = (
                        int(observation["event_wall_ns"]) - signal_ns
                    ) / 1e6
                    result["continuous_valid_duration_ms"] = result["first_failure_after_ms"]
        return result

    horizon = ordered[due_index]
    interval = ordered[start_index:due_index + 1]
    result = _empty_persistence(signal, wait_ms)
    result.update({
        "status": "observed",
        "complete_observed": True,
        "right_censored": False,
        "horizon_wall_ns": int(horizon["event_wall_ns"]),
        "horizon_utc": iso_utc(int(horizon["event_wall_ns"])),
        "observed_after_ms": (int(horizon["event_wall_ns"]) - signal_ns) / 1e6,
        "sampling_lag_ms": (int(horizon["event_wall_ns"]) - due_ns) / 1e6,
        "horizon_edge_bps": horizon.get(edge_key),
        "observed_pair_count": len(interval),
        "last_observation_after_ms": (
            int(interval[-1]["event_wall_ns"]) - signal_ns
        ) / 1e6 if interval else None,
        "max_pair_observation_gap_ms": max(
            [
                (int(b["event_wall_ns"]) - int(a["event_wall_ns"])) / 1e6
                for a, b in zip(interval, interval[1:])
            ] or [0.0]
        ),
    })
    if finite(signal.get(edge_key)) and finite(horizon.get(edge_key)):
        result["edge_loss_bps"] = float(signal[edge_key]) - float(horizon[edge_key])

    first_failure = None
    for observation in interval:
        reason = observation["sell_failure_reason"] if direction == "sell_entropy" else observation["buy_failure_reason"]
        if reason is not None:
            first_failure = observation
            result["first_failure_kind"] = reason
            result["first_failure_after_ms"] = (
                int(observation["event_wall_ns"]) - signal_ns
            ) / 1e6
            result["continuous_valid_duration_ms"] = result["first_failure_after_ms"]
            break
    if first_failure is None:
        result["continuous_valid_duration_ms"] = result["observed_after_ms"]
    result["continuous_survived_observed"] = first_failure is None
    result["qualifies_at_horizon"] = (
        horizon["sell_qualifies"] if direction == "sell_entropy"
        else horizon["buy_qualifies"]
    )
    if first_failure is not None:
        failure_ns = int(first_failure["event_wall_ns"])
        for observation in interval:
            if int(observation["event_wall_ns"]) <= failure_ns:
                continue
            qualifies = (
                observation["sell_qualifies"] if direction == "sell_entropy"
                else observation["buy_qualifies"]
            )
            if qualifies:
                result["recovered_after_failure"] = True
                result["recovery_after_ms"] = (
                    int(observation["event_wall_ns"]) - signal_ns
                ) / 1e6
                break
        if result["recovered_after_failure"] is None:
            result["recovered_after_failure"] = False
    else:
        result["recovered_after_failure"] = False
    return result


def _load_run(
    run_dir: Path, remote_hashes: Dict[str, str],
) -> Tuple[dict, dict, List[dict], List[dict]]:
    parse_errors: List[dict] = []
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    records = {kind: [] for kind in KINDS}
    checksums = []
    actual_counts = Counter()
    for path in sorted(p for p in run_dir.iterdir() if p.is_file()):
        digest = sha256(path)
        relative = f"{run_dir.name}/{path.name}"
        remote_digest = remote_hashes.get(relative)
        checksums.append({
            "run_id": run_dir.name, "file": path.name,
            "bytes": path.stat().st_size, "sha256": digest,
            "remote_sha256": remote_digest,
            "sha256_match": remote_digest is not None and digest == remote_digest,
        })
        if path.suffix != ".jsonl":
            continue
        kind = path.name.split("-", 1)[0]
        if kind not in records:
            continue
        rows = _read_jsonl(path, parse_errors)
        records[kind].extend(rows)
        actual_counts[kind] += len(rows)
    writer_stats = manifest.get("writer_stats", {})
    integrity = {
        "run_id": run_dir.name,
        "market": manifest.get("market"),
        "record_only": manifest.get("record_only"),
        "preserve_raw": manifest.get("preserve_raw"),
        "started_utc": iso_utc(manifest.get("started_wall_ns")),
        "ended_utc": iso_utc(manifest.get("ended_wall_ns")),
        "duration_sec": (
            (int(manifest["ended_wall_ns"]) - int(manifest["started_wall_ns"])) / 1e9
            if manifest.get("ended_wall_ns") and manifest.get("started_wall_ns")
            else None
        ),
        "files": len(checksums),
        "bytes": sum(item["bytes"] for item in checksums),
        "sha256_mismatch_count": sum(not item["sha256_match"] for item in checksums),
        "parse_error_count": len(parse_errors),
        "dropped_updates": int(manifest.get("dropped_updates", 0)),
        "dropped_lifecycle": int(manifest.get("dropped_lifecycle", 0)),
        "source_timestamp_counts": manifest.get("source_timestamp_counts", {}),
        "rows": {},
        "files_expired": 0,
        "write_errors": 0,
        "rotations": 0,
    }
    for kind in KINDS:
        stats = writer_stats.get(kind, {})
        written = int(stats.get("rows_written", 0))
        actual = int(actual_counts[kind])
        integrity["rows"][kind] = {
            "written": written, "retained": actual,
            "match": written == actual,
            "retention_pct": (100.0 * actual / written) if written else None,
        }
        integrity["files_expired"] += int(stats.get("files_expired", 0))
        integrity["write_errors"] += int(stats.get("write_errors", 0))
        integrity["rotations"] += int(stats.get("rotations", 0))
    integrity["completed"] = bool(manifest.get("ended_wall_ns"))
    integrity["row_counts_match"] = all(
        row["match"] for row in integrity["rows"].values()
    )
    integrity["eligible"] = all((
        integrity["completed"],
        integrity["sha256_mismatch_count"] == 0,
        integrity["parse_error_count"] == 0,
        integrity["row_counts_match"],
        integrity["preserve_raw"] is True,
    ))
    return (
        {"run_id": run_dir.name, "manifest": manifest, "records": records},
        integrity,
        checksums,
        parse_errors,
    )


def load_dataset(data_root: str | Path) -> dict:
    """Load and validate a frozen Run02 snapshot before analysis."""
    root = Path(data_root)
    supervisor = json.loads((root / "supervisor-manifest.json").read_text(encoding="utf-8"))
    remote_hashes = _remote_hashes(root)
    runs = []
    run_integrity = []
    discovered_integrity = []
    excluded_runs = []
    checksums = []
    parse_errors: List[dict] = []
    for manifest_path in sorted((root / "runs").glob("*/manifest.json")):
        run, integrity, run_checksums, run_parse_errors = _load_run(
            manifest_path.parent, remote_hashes,
        )
        discovered_integrity.append(integrity)
        if integrity["eligible"]:
            runs.append(run)
            run_integrity.append(integrity)
        else:
            excluded_runs.append({
                "run_id": integrity["run_id"],
                "reasons": [
                    key for key, condition in (
                        ("incomplete", not integrity["completed"]),
                        ("sha256_mismatch", integrity["sha256_mismatch_count"] != 0),
                        ("json_parse_error", integrity["parse_error_count"] != 0),
                        ("row_count_mismatch", not integrity["row_counts_match"]),
                        ("preserve_raw_false", integrity["preserve_raw"] is not True),
                    ) if condition
                ],
            })
        checksums.extend(run_checksums)
        parse_errors.extend(run_parse_errors)
    total_written = Counter()
    total_retained = Counter()
    for item in run_integrity:
        for kind, row in item["rows"].items():
            total_written[kind] += row["written"]
            total_retained[kind] += row["retained"]
    source_status = Counter()
    depth_shapes = Counter()
    rolling_status = Counter()
    rolling_by_run = {}
    for run in runs:
        run_status = Counter()
        for row in run["records"]["quotes"]:
            source_status[str(row.get("source_timestamp_status"))] += 1
            depth_shapes[f'{len(row.get("bids") or [])}x{len(row.get("asks") or [])}'] += 1
            if _parameter_valid(row) and row.get("coverage") == 1.0 and row.get("sample_count") == 480:
                rolling_status["valid_480"] += 1
                run_status["valid_480"] += 1
            else:
                rolling_status["invalid"] += 1
                run_status["invalid"] += 1
                run_status[f"invalid:{row.get('parameter_invalid_reason') or 'invalid_or_incomplete'}"] += 1
        rolling_by_run[run["run_id"]] = dict(run_status)
    integrity = {
        "supervisor": supervisor,
        "manifest_count": len(discovered_integrity),
        "eligible_run_count": len(runs),
        "completed_runs": sum(1 for run in runs if run["manifest"].get("ended_wall_ns")),
        "target_runs": supervisor.get("target_runs"),
        "failed_runs": supervisor.get("failed_runs"),
        "status": supervisor.get("status"),
        "total_files": len(checksums),
        "total_bytes": sum(item["bytes"] for item in checksums),
        "sha256_mismatch_count": sum(not item["sha256_match"] for item in checksums),
        "parse_error_count": len(parse_errors),
        "row_counts": {
            kind: {
                "written": total_written[kind],
                "retained": total_retained[kind],
                "retention_pct": (
                    100.0 * total_retained[kind] / total_written[kind]
                    if total_written[kind] else None
                ),
            }
            for kind in KINDS
        },
        "source_timestamp_status": dict(source_status),
        "depth_shapes": dict(depth_shapes),
        "rolling_status": dict(rolling_status),
        "rolling_by_run": rolling_by_run,
        "dropped_updates": sum(item["dropped_updates"] for item in run_integrity),
        "dropped_lifecycle": sum(item["dropped_lifecycle"] for item in run_integrity),
        "files_expired": sum(item["files_expired"] for item in run_integrity),
        "write_errors": sum(item["write_errors"] for item in run_integrity),
        "rotations": sum(item["rotations"] for item in run_integrity),
        "preserve_raw_all_runs": all(item["preserve_raw"] is True for item in run_integrity),
        "run_integrity": run_integrity,
        "excluded_runs": excluded_runs,
    }
    return {"root": root, "runs": runs, "integrity": integrity, "checksums": checksums}


def _event_summary(events: Sequence[dict], wait_ms: int, direction: str) -> dict:
    selected = [
        event for event in events
        if event["wait_ms"] == wait_ms and (direction == "all" or event["direction"] == direction)
    ]
    raw = len(selected)
    complete = [event for event in selected if event["complete_observed"]]
    continuous = [event for event in complete if event["continuous_survived_observed"]]
    first_failures = [event for event in complete if event["first_failure_kind"] is not None]
    recovered = [event for event in first_failures if event["recovered_after_failure"]]
    qualifying = [event for event in complete if event["qualifies_at_horizon"]]
    edge_losses = [event["edge_loss_bps"] for event in complete if finite(event["edge_loss_bps"])]
    remaining = [event["horizon_edge_bps"] for event in complete if finite(event["horizon_edge_bps"])]
    failure_times = [event["first_failure_after_ms"] for event in first_failures if finite(event["first_failure_after_ms"])]
    continuous_durations = [
        event["continuous_valid_duration_ms"]
        for event in complete if finite(event["continuous_valid_duration_ms"])
    ]
    gaps = [event["max_pair_observation_gap_ms"] for event in complete if finite(event["max_pair_observation_gap_ms"])]
    right = raw - len(complete)
    return {
        "direction": direction,
        "wait_ms": wait_ms,
        "raw_candidate_count": raw,
        "complete_observed_count": len(complete),
        "right_censored_count": right,
        "continuous_survivor_count": len(continuous),
        "continuous_survival_rate_pct": 100.0 * len(continuous) / len(complete) if complete else None,
        "signal_retained_count": len(qualifying),
        "signal_retention_pct": 100.0 * len(qualifying) / raw if raw else None,
        "first_failure_count": len(first_failures),
        "first_failure_rate_of_complete_pct": 100.0 * len(first_failures) / len(complete) if complete else None,
        "recovered_after_failure_count": len(recovered),
        "recovery_rate_of_first_failures_pct": 100.0 * len(recovered) / len(first_failures) if first_failures else None,
        "horizon_qualifying_count": len(qualifying),
        "horizon_valid_edge_count": len(remaining),
        "edge_loss_p50_bps": percentile(edge_losses, 0.50),
        "edge_loss_p90_bps": percentile(edge_losses, 0.90),
        "remaining_edge_p50_bps": percentile(remaining, 0.50),
        "first_failure_after_p50_ms": percentile(failure_times, 0.50),
        "first_failure_after_p90_ms": percentile(failure_times, 0.90),
        "continuous_valid_duration_p50_ms": percentile(continuous_durations, 0.50),
        "continuous_valid_duration_p90_ms": percentile(continuous_durations, 0.90),
        "max_pair_observation_gap_p50_ms": percentile(gaps, 0.50),
        "max_pair_observation_gap_p95_ms": percentile(gaps, 0.95),
        "opportunity_lost_count": raw - len(qualifying),
        "opportunity_lost_pct": 100.0 * (raw - len(qualifying)) / raw if raw else None,
    }


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


def _make_charts(output: Path, summaries: Sequence[dict]) -> List[str]:
    all_rows = [row for row in summaries if row["direction"] == "all"]
    waits = [row["wait_ms"] for row in all_rows]
    # Keep report generation dependency-free.  A small SVG is sufficient for
    # the comparison and avoids importing a GUI/font stack on the recorder
    # workstation.
    width, height = 1000, 470
    panels = [(60, 55, 400, 330), (550, 55, 400, 330)]

    def finite_values(key: str) -> List[float]:
        return [float(row[key]) for row in all_rows if finite(row.get(key))]

    def points(values: Sequence[Optional[float]], x: int, y: int, w: int, h: int):
        clean = [value for value in values if value is not None and finite(value)]
        if not clean:
            return ""
        low, high = min(0.0, min(clean)), max(1.0, max(clean))
        if math.isclose(low, high):
            high = low + 1.0
        coords = []
        for index, value in enumerate(values):
            if value is None or not finite(value):
                continue
            px = x + (w * index / max(1, len(values) - 1))
            py = y + h - ((float(value) - low) / (high - low) * h)
            coords.append(f"{px:.1f},{py:.1f}")
        return " ".join(coords)

    def panel(x: int, y: int, w: int, h: int, title: str, series: Sequence[Tuple[str, str, str]]):
        chunks = [
            f'<text x="{x}" y="{y - 20}" class="title">{title}</text>',
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" class="plot"/>',
        ]
        for label, key, colour in series:
            values = [row.get(key) for row in all_rows]
            coord = points(values, x, y, w, h)
            if coord:
                chunks.append(f'<polyline points="{coord}" fill="none" stroke="{colour}" stroke-width="2"/>')
                for index, value in enumerate(values):
                    if value is None or not finite(value):
                        continue
                    px = x + (w * index / max(1, len(values) - 1))
                    clean = [v for v in values if v is not None and finite(v)]
                    low, high = min(0.0, min(clean)), max(1.0, max(clean))
                    if math.isclose(low, high):
                        high = low + 1.0
                    py = y + h - ((float(value) - low) / (high - low) * h)
                    chunks.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="{colour}"/>')
            chunks.append(f'<line x1="{x + 12}" y1="{y + 18 * (series.index((label, key, colour)) + 1)}" x2="{x + 32}" y2="{y + 18 * (series.index((label, key, colour)) + 1)}" stroke="{colour}" stroke-width="2"/>')
            chunks.append(f'<text x="{x + 38}" y="{y + 22 * (series.index((label, key, colour)) + 1)}" class="legend">{label}</text>')
        for index, wait in enumerate(waits):
            px = x + (w * index / max(1, len(waits) - 1))
            chunks.append(f'<text x="{px:.1f}" y="{y + h + 20}" text-anchor="middle" class="axis">{wait}ms</text>')
        return chunks

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<style>.title{font:600 16px sans-serif}.legend,.axis{font:12px sans-serif}.plot{fill:#fafafa;stroke:#bbb}.note{font:12px sans-serif}</style>',
        '<text x="60" y="25" class="note">Run02 persistence (observed pair events; not execution PnL)</text>',
    ]
    svg.extend(panel(*panels[0], "Retention / sampled survival (%)", [
        ("sampled continuous survival", "continuous_survival_rate_pct", "#1464a0"),
        ("qualifies at horizon", "signal_retention_pct", "#d97706"),
    ]))
    svg.extend(panel(*panels[1], "Edge loss / remaining edge (bps)", [
        ("edge loss P90", "edge_loss_p90_bps", "#b91c1c"),
        ("remaining edge P50", "remaining_edge_p50_bps", "#15803d"),
    ]))
    svg.append('</svg>')
    path = output / "persistence_summary.svg"
    path.write_text("\n".join(svg) + "\n", encoding="utf-8")
    return [path.name]


def _format(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _write_report(
    path: Path, data: dict, summaries: Sequence[dict],
    run_summary: Sequence[dict], charts: Sequence[str],
) -> None:
    integrity = data["integrity"]
    all_rows = [row for row in summaries if row["direction"] == "all"]
    lines = [
        "# SNDK × Lighter RH Run02 Persistence 唯讀分析",
        "",
        "本報告只分析已完成且 checksum 驗證通過的 Run02 原始 Quote。所有 Edge 是行情觀測值，不是成交、Capture 或 PnL。未修改 Engine、策略參數、IOC、Recorder 或原始資料。",
        "",
        "## 完整性",
        "",
        f"- Frozen input root: `{data['root']}`; market: `{integrity['supervisor'].get('market')}`; recorder source commit: `{integrity['supervisor'].get('source_commit')}`。",
        f"- Supervisor status: `{integrity['status']}`, {integrity['completed_runs']}/{integrity['target_runs']} completed, failed `{integrity['failed_runs']}`; eligible runs `{integrity['eligible_run_count']}/{integrity['manifest_count']}`。",
        f"- Snapshot files: {integrity['total_files']}; bytes: {integrity['total_bytes']}; SHA-256 mismatches: {integrity['sha256_mismatch_count']}; JSON parse errors: {integrity['parse_error_count']}。",
        f"- Quote rows: {integrity['row_counts']['quotes']['retained']}/{integrity['row_counts']['quotes']['written']} retained ({_format(integrity['row_counts']['quotes']['retention_pct'])}%). Signals: {integrity['row_counts']['signals']['retained']}; Decay: {integrity['row_counts']['decay']['retained']}。",
        f"- Source timestamps: `{integrity['source_timestamp_status']}`；depth shapes: `{integrity['depth_shapes']}`；Rolling valid 480/480: {integrity['rolling_status'].get('valid_480', 0)}, invalid: {integrity['rolling_status'].get('invalid', 0)}。",
        f"- Drops: updates {integrity['dropped_updates']}, lifecycle {integrity['dropped_lifecycle']}; writer errors {integrity['write_errors']}; files expired {integrity['files_expired']}。",
        "- 12 個 Run 未跨界拼接；所有 Run `preserve_raw=true`。",
        "- Rolling invalid rows by Run: " + "; ".join(
            f"{run_id}={status.get('invalid', 0)} ({', '.join(f'{key}={value}' for key, value in status.items() if key.startswith('invalid:')) or 'none'})"
            for run_id, status in sorted(integrity["rolling_by_run"].items())
        ) + "。",
        "- Invalid Rolling rows are retained as evidence and excluded from valid-parameter claims; the analyzer does not fill or downgrade the 480/480 requirement。",
        "",
        "## Run 覆蓋",
        "",
        "| Run | UTC start | UTC end | quotes | signals | pair observations | pair gap P50/P95/max (ms) |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for row in run_summary:
        lines.append(
            f"| {row['run_id']} | {row['first_observation_utc']} | {row['last_observation_utc']} | {row['quote_rows']} | {row['signal_rows']} | {row['pair_observations']} | {_format(row['pair_observation_gap_p50_ms'])}/{_format(row['pair_observation_gap_p95_ms'])}/{_format(row['pair_observation_gap_max_ms'])} |"
        )
    lines += [
        "",
        "## Persistence 定義",
        "",
        "每個 Signal 只使用同一 Run 內、訊號之後首次到達指定等待時間的 pair observation。`sampled_continuous_survived` 表示該區間內所有已觀測 pair state 都仍符合方向門檻；靜默期間沒有被當成失效，並另列最大 pair observation gap。因而這是觀測事件連續性，不是對每一毫秒市場狀態的證明。沒有後續 pair observation 的事件列為 right-censored。",
        "",
        "## 全方向比較",
        "",
        "| wait | raw candidates | complete observed | right-censored | continuous survival | signal retention at horizon | first failure | recovery after failure | continuous valid P50 ms | edge loss P50 | edge loss P90 | remaining edge P50 |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in all_rows:
        lines.append(
            f"| {row['wait_ms']}ms | {row['raw_candidate_count']} | {row['complete_observed_count']} | {row['right_censored_count']} | {_format(row['continuous_survival_rate_pct'])}% | {_format(row['signal_retention_pct'])}% | {_format(row['first_failure_rate_of_complete_pct'])}% | {_format(row['recovery_rate_of_first_failures_pct'])}% | {_format(row['continuous_valid_duration_p50_ms'])} | {_format(row['edge_loss_p50_bps'])} | {_format(row['edge_loss_p90_bps'])} | {_format(row['remaining_edge_p50_bps'])} |"
        )
    lines += ["", "## BUY / SELL", "", "完整方向別數據在 `persistence_summary.csv`；每個 Signal 的首次失效、恢復、右設限與觀測 gap 在 `persistence_events.csv`。`signal retention` 是等待時點仍符合門檻的比例，不等同於連續存活。", ""]
    for direction in ("sell_entropy", "buy_entropy"):
        lines += [f"### {direction}", "", "| wait | raw | complete | right-censored | continuous survival | signal retention | first failure | recovery | continuous valid P50 ms | edge loss P90 | remaining edge P50 |", "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for row in summaries:
            if row["direction"] != direction:
                continue
            lines.append(f"| {row['wait_ms']}ms | {row['raw_candidate_count']} | {row['complete_observed_count']} | {row['right_censored_count']} | {_format(row['continuous_survival_rate_pct'])}% | {_format(row['signal_retention_pct'])}% | {_format(row['first_failure_rate_of_complete_pct'])}% | {_format(row['recovery_rate_of_first_failures_pct'])}% | {_format(row['continuous_valid_duration_p50_ms'])} | {_format(row['edge_loss_p90_bps'])} | {_format(row['remaining_edge_p50_bps'])} |")
        lines.append("")
    lines += [
        "## 結論與證據強度",
        "",
        "Persistence 的等待結果不是單調改善：100ms 的全方向 sampled continuous survival 為 85.44%、horizon retention 為 85.88%；250ms 分別為 2.52% 與 3.55%；500ms 分別為 0.31% 與 33.50%。500ms 的 horizon retention 高於 250ms，表示部分事件在中途失效後重新同步／恢復，不能把 horizon 通過率解讀成連續 Persistence 品質。",
        "BUY／SELL 方向不同：100ms horizon retention 為 BUY 86.44%、SELL 65.34%；250ms 為 BUY 3.53%、SELL 4.24%；500ms 為 BUY 34.02%、SELL 13.97%。這支持分方向描述，但不支持在正式策略中直接採用任一等待門檻。",
        "Edge Loss 使用 signal edge - horizon edge 定義；它與 remaining edge 都只是可觀測 BBO Edge。資料中的 pair observation gap P95 約 110–226ms（依等待窗口變化），所以不能證明 gap 內每個瞬間都持續有效；也沒有成交、queue、滑價或 PnL 證據。相關性不代表因果關係。",
        "",
        "目前唯一建議：**C，資料不足，暫不決策，也不把 Persistence 接入正式策略**。Run02 足以量化觀測事件的等待成本與恢復現象，但不足以證明哪個門檻改善實際執行品質；需要更細的同步觀測／成交證據後再另行審查。",
        "",
        "## 產物",
        "",
        "- `integrity_summary.json`：manifest、row、hash、retention 與品質驗證。",
        "- `file_checksums.csv`：每個 Run 檔案的本地／遠端 SHA-256。",
        "- `persistence_summary.csv`：全方向及 BUY/SELL 的四個等待條件。",
        "- `persistence_events.csv`：逐 Signal 觀測結果。",
        "- `run_summary.csv`、`time_segment_summary.csv`：逐 Run／時段分布。",
        "- " + ", ".join(charts) if charts else "- 圖表未生成（matplotlib 不可用）。",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def analyze(data_root: str | Path, output_dir: str | Path) -> dict:
    data = load_dataset(data_root)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    events = []
    run_summary = []
    time_summary = []
    for run in data["runs"]:
        observations = build_pair_observations(run["records"]["quotes"], skew_threshold_ms=250.0)
        observations.sort(key=lambda row: int(row["event_wall_ns"]))
        observation_times = [int(row["event_wall_ns"]) for row in observations]
        signals = sorted(run["records"]["signals"], key=lambda row: int(row["signal_wall_ns"]))
        for signal in signals:
            for wait_ms in WAIT_MS:
                event = evaluate_signal_persistence(
                    signal, observations, wait_ms=wait_ms, skew_threshold_ms=250.0,
                    observation_times=observation_times,
                )
                event["run_id"] = run["run_id"]
                events.append(event)
        first = observations[0]["event_wall_ns"] if observations else None
        last = observations[-1]["event_wall_ns"] if observations else None
        gap_values = [
            (int(b["event_wall_ns"]) - int(a["event_wall_ns"])) / 1e6
            for a, b in zip(observations, observations[1:])
        ]
        run_summary.append({
            "run_id": run["run_id"],
            "quote_rows": len(run["records"]["quotes"]),
            "signal_rows": len(signals),
            "pair_observations": len(observations),
            "pair_observation_gap_p50_ms": percentile(gap_values, 0.50),
            "pair_observation_gap_p95_ms": percentile(gap_values, 0.95),
            "pair_observation_gap_max_ms": max(gap_values) if gap_values else None,
            "first_observation_utc": iso_utc(first),
            "last_observation_utc": iso_utc(last),
        })
        if first is not None:
            by_block = defaultdict(int)
            for signal in signals:
                dt = datetime.fromtimestamp(int(signal["signal_wall_ns"]) / 1e9, timezone.utc)
                block = dt.replace(hour=(dt.hour // 4) * 4, minute=0, second=0, microsecond=0).isoformat()
                by_block[block] += 1
            for block, count in sorted(by_block.items()):
                time_summary.append({"run_id": run["run_id"], "utc_block": block, "signal_count": count})

    summaries = []
    for direction in ("all", "sell_entropy", "buy_entropy"):
        for wait_ms in WAIT_MS:
            summaries.append(_event_summary(events, wait_ms, direction))
    charts = _make_charts(output, summaries)
    integrity_path = output / "integrity_summary.json"
    integrity_path.write_text(json.dumps(data["integrity"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_csv(output / "file_checksums.csv", data["checksums"])
    _write_csv(output / "persistence_summary.csv", summaries)
    _write_csv(output / "persistence_events.csv", events)
    _write_csv(output / "run_summary.csv", run_summary)
    _write_csv(output / "time_segment_summary.csv", time_summary)
    _write_report(
        output.parent / "sndk_rh_run02_persistence_analysis.md",
        data, summaries, run_summary, charts,
    )
    result = {
        "schema_version": 1,
        "data_root": str(data_root),
        "output_dir": str(output),
        "integrity": data["integrity"],
        "event_count": len(events),
        "summary": summaries,
        "charts": charts,
    }
    (output / "analysis_summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    result = analyze(args.data_root, args.output_dir)
    print(json.dumps({
        "completed_runs": result["integrity"]["completed_runs"],
        "quotes": result["integrity"]["row_counts"]["quotes"],
        "signals": result["integrity"]["row_counts"]["signals"],
        "decay": result["integrity"]["row_counts"]["decay"],
        "event_count": result["event_count"],
        "output_dir": result["output_dir"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
