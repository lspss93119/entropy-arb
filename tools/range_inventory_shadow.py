#!/usr/bin/env python3
"""Run the frozen range-inventory strategy as a recorder sidecar.

This process never imports or calls live venue execution code.  It reads the
minute recorder CSV, keeps a synthetic position/cash ledger, and writes two
shadow variants side-by-side:

  baseline    frozen 4h-long / 2h-short strategy
  range_gate  same strategy with the 4h Q90-Q10 >= 10 bps exposure gate

On a new forward run, ``--start latest`` (default) uses all earlier rows only as
percentile warmup, starts synthetic PnL flat at the latest completed minute,
and queues that minute's signal for execution on the next completed minute.
The chosen start minute is persisted next to the output, so restarts rebuild the
same forward history instead of silently resetting PnL.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

# Allow execution from the repository root without package installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.range_inventory_shadow import (  # noqa: E402
    DEFAULT_PARAMS,
    SHADOW_CSV_HEADER,
    RangeInventoryShadow,
    load_recorder_rows,
    result_to_csv_row,
)

STATE_SCHEMA_VERSION = 1


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temp, path)


def _state_path(output: Path, explicit: str | None) -> Path:
    return Path(explicit) if explicit else Path(str(output) + ".state.json")


def _new_state(source: Path, rows: list[dict[str, str]], start_mode: str) -> dict:
    if not rows:
        raise RuntimeError("recorder CSV contains no data rows")
    start_ts = (
        float(rows[-1]["minute_ts"])
        if start_mode == "latest"
        else float(rows[0]["minute_ts"])
    )
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "source_csv": str(source.resolve()),
        "start_mode": start_mode,
        "start_signal_minute_ts": start_ts,
        "start_signal_time_utc": next(
            row["time_utc"] for row in rows
            if float(row["minute_ts"]) == start_ts
        ),
        "funding_included": False,
        "variants": {
            "baseline": {"use_range_gate": False},
            "range_gate": {"use_range_gate": True},
        },
        "frozen_params": asdict(DEFAULT_PARAMS),
    }


def _load_or_create_state(source: Path, output: Path, state_path: Path,
                          start_mode: str, reset: bool,
                          rows: list[dict[str, str]]) -> dict:
    if state_path.exists() and not reset:
        state = json.loads(state_path.read_text())
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise RuntimeError("unsupported shadow state schema")
        expected_source = str(source.resolve())
        if state.get("source_csv") != expected_source:
            raise RuntimeError(
                "shadow state belongs to another recorder CSV; use --reset-start"
            )
        if state.get("frozen_params") != asdict(DEFAULT_PARAMS):
            raise RuntimeError(
                "frozen shadow parameters changed since this run started; "
                "use a new output path or --reset-start"
            )
        return state

    state = _new_state(source, rows, start_mode)
    _atomic_json_write(state_path, state)
    return state


def _build(rows: list[dict[str, str]], start_ts: float, output: Path):
    baseline = RangeInventoryShadow(
        variant="baseline", use_range_gate=False
    )
    gated = RangeInventoryShadow(
        variant="range_gate", use_range_gate=True
    )
    shadows = (baseline, gated)

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(SHADOW_CSV_HEADER)
        for row in rows:
            ts = float(row["minute_ts"])
            if ts < start_ts:
                for shadow in shadows:
                    shadow.warmup_row(row)
                continue
            for shadow in shadows:
                writer.writerow(result_to_csv_row(shadow.on_row(row)))
        fh.flush()
    return shadows


def _summary(shadows) -> str:
    parts = []
    for shadow in shadows:
        # Exact current inventory/equity are written to the output.  The live
        # console summary focuses on stable cumulative fields available on the
        # shadow object itself.
        parts.append(
            f"{shadow.variant}: turnover=${shadow.turnover_usd:,.0f} "
            f"closed_long=${shadow.long_realized_usd:+.2f} "
            f"closed_short=${shadow.short_realized_usd:+.2f}"
        )
    return " | ".join(parts)


def _append_live_row(output_fh, writer, shadows, row):
    action_lines = []
    for shadow in shadows:
        result = shadow.on_row(row)
        writer.writerow(result_to_csv_row(result))
        if result.action not in ("none", "stale_signal_skipped"):
            action_lines.append(
                f"{result.time_utc} {result.variant} {result.action} "
                f"${result.trade_notional_usd:.0f} "
                f"inv=${result.inventory_usd:+.0f} "
                f"equity=${result.equity_usd:+.2f}"
            )
    output_fh.flush()
    for line in action_lines:
        print(line, flush=True)


def _follow(source: Path, output: Path, shadows, poll_seconds: float) -> None:
    with source.open("r", newline="") as source_fh, output.open("a", newline="") as out_fh:
        header_line = source_fh.readline()
        if not header_line:
            raise RuntimeError("recorder CSV has no header")
        header = next(csv.reader([header_line]))
        source_fh.seek(0, os.SEEK_END)
        inode = os.fstat(source_fh.fileno()).st_ino
        writer = csv.writer(out_fh)

        print("following recorder; Ctrl-C to stop", flush=True)
        while True:
            position = source_fh.tell()
            line = source_fh.readline()
            if not line:
                try:
                    stat = source.stat()
                except FileNotFoundError:
                    stat = None
                if stat is None or stat.st_ino != inode or stat.st_size < position:
                    raise RuntimeError(
                        "recorder CSV rotated or truncated; restart the shadow "
                        "runner so state is rebuilt deterministically"
                    )
                time.sleep(poll_seconds)
                continue
            if not line.endswith("\n"):
                source_fh.seek(position)
                time.sleep(poll_seconds)
                continue
            values = next(csv.reader([line]))
            if len(values) != len(header):
                print("ignored malformed recorder row", file=sys.stderr, flush=True)
                continue
            row = dict(zip(header, values))
            try:
                _append_live_row(out_fh, writer, shadows, row)
            except ValueError as exc:
                print(f"ignored invalid recorder row: {exc}", file=sys.stderr, flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forward shadow for the frozen ANTH range-inventory strategy"
    )
    parser.add_argument("--csv", required=True, help="minute recorder CSV")
    parser.add_argument(
        "--output",
        default="logs/shadow/range-inventory.csv",
        help="shadow result CSV (default: logs/shadow/range-inventory.csv)",
    )
    parser.add_argument(
        "--state",
        default=None,
        help="state metadata path (default: <output>.state.json)",
    )
    parser.add_argument(
        "--start",
        choices=("latest", "first"),
        default="latest",
        help="new run start: latest completed minute for forward validation, "
             "or first row for historical replay",
    )
    parser.add_argument(
        "--reset-start",
        action="store_true",
        help="replace prior start metadata and restart shadow PnL flat",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="keep following newly appended recorder rows",
    )
    parser.add_argument(
        "--poll-seconds", type=float, default=1.0,
        help="follow polling interval (default: 1.0)",
    )
    args = parser.parse_args()

    source = Path(args.csv)
    output = Path(args.output)
    state_path = _state_path(output, args.state)
    if not source.exists():
        raise SystemExit(f"recorder CSV not found: {source}")
    if args.poll_seconds <= 0:
        raise SystemExit("--poll-seconds must be positive")

    rows = load_recorder_rows(str(source))
    state = _load_or_create_state(
        source, output, state_path, args.start, args.reset_start, rows
    )
    start_ts = float(state["start_signal_minute_ts"])
    shadows = _build(rows, start_ts, output)

    print(
        f"shadow start={state['start_signal_time_utc']} "
        f"funding=excluded friction=0.5bps/leg output={output}",
        flush=True,
    )
    print(_summary(shadows), flush=True)

    if args.follow:
        try:
            _follow(source, output, shadows, args.poll_seconds)
        except KeyboardInterrupt:
            print("shadow stopped", flush=True)


if __name__ == "__main__":
    main()
