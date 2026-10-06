#!/usr/bin/env python3
"""Offline parity: pinned original shadow vs shared core and live signal path.

No .env, clients, network or orders. Replay is a minute-BBO T+1 execution
proxy, NOT a live fill/PnL forecast. Compare every field, target and action,
not only final totals. The live path uses identical simulated inventory;
immediate actual live execution intentionally has different timing.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import types
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.range_inventory import DEFAULT_PARAMS  # noqa: E402
from entropy_arb.range_inventory_live import CANARY_PARAMS, RangeInventoryLive  # noqa: E402
from entropy_arb.range_inventory_shadow import RangeInventoryShadow  # noqa: E402

ORACLE_SHA = "a81eb822baa1d27ce609c4a5b95fae0a3b383a59"
ROOT = Path(__file__).resolve().parents[1]


def load_oracle():
    source = subprocess.check_output([
        "git", "show", f"{ORACLE_SHA}:entropy_arb/range_inventory_shadow.py",
    ], cwd=ROOT, text=True)
    module = types.ModuleType("range_inventory_pinned_oracle")
    sys.modules[module.__name__] = module
    exec(compile(source, "<exact-shadow-git-oracle>", "exec"), module.__dict__)
    return module


def compare_fields(expected: dict, actual: dict) -> None:
    if expected.keys() != actual.keys():
        raise AssertionError("parity schema divergence")
    for key, value in expected.items():
        other = actual[key]
        value = None if value == "" else value
        other = None if other == "" else other
        if value == other:
            continue
        try:
            equal = math.isclose(float(value), float(other), rel_tol=0, abs_tol=1e-8)
        except (TypeError, ValueError):
            equal = False
        if not equal:
            raise AssertionError(f"{key} divergence at minute {actual.get('minute_ts')}: {value!r} != {other!r}")


def expected_signal_action(inventory, target, blocked):
    if blocked or target is None:
        return "blocked"
    if abs(target - inventory) <= 1e-9:
        return "none"
    if inventory > 1e-9:
        return "long_build" if target > inventory else "long_release"
    if inventory < -1e-9:
        return "short_build" if target < inventory else "short_cover"
    return "long_build" if target > 0 else "short_build"


def check_parity(rows: list[dict], *, params=DEFAULT_PARAMS, reference_rows=None):
    if not rows:
        raise ValueError("empty recorder input")
    old = load_oracle()
    old_params = old.FrozenRangeInventoryParams(**asdict(params))
    before = [old.RangeInventoryShadow(variant=name, use_range_gate=gate, params=old_params)
              for name, gate in (("baseline", False), ("range_gate", True))]
    after = [RangeInventoryShadow(variant=name, use_range_gate=gate, params=params)
             for name, gate in (("baseline", False), ("range_gate", True))]
    # The independent simulated executor is the pinned pre-refactor T+1
    # executor. It receives targets only from the actual live adapter path.
    sim = old.RangeInventoryShadow(variant="range_gate", use_range_gate=True, params=old_params)
    live = RangeInventoryLive("unused-offline-state.json", symbol="ANTH", hedge="lighter-rh", params=params)
    final, count, target_count = {}, 0, 0
    for row in rows:
        old_results = [asdict(s.on_row(row)) for s in before]
        new_results = [asdict(s.on_row(row)) for s in after]
        for expected, actual in zip(old_results, new_results):
            compare_fields(expected, actual)
            if reference_rows is not None:
                if count >= len(reference_rows):
                    raise AssertionError("reference row count divergence")
                compare_fields(reference_rows[count], actual)
            final[actual["variant"]] = actual
            count += 1
        normalized = live.core._row(row)
        executed_ts, executed_action, notional = sim._execute(normalized)
        inventory = sim._inventory(normalized)
        signal = live.signal_for_inventory(row, inventory_usd=inventory)
        expected = old_results[1]
        compare_fields({
            "minute_ts": expected["minute_ts"],
            "target": expected["signal_target_usd"],
            "action": expected_signal_action(expected["inventory_usd"],
                                              expected["signal_target_usd"],
                                              expected["signal_exposure_blocked"]),
            "executed_signal_ts": expected["executed_signal_ts"],
            "executed_action": expected["action"],
            "trade_notional": expected["trade_notional_usd"],
            "inventory": expected["inventory_usd"],
        }, {
            "minute_ts": signal.minute_ts, "target": signal.target_usd, "action": signal.action,
            "executed_signal_ts": executed_ts, "executed_action": executed_action,
            "trade_notional": notional, "inventory": inventory,
        })
        sim._pending = None if signal.target_usd is None else (signal.minute_ts, signal.target_usd)
        target_count += 1
    if reference_rows is not None and count != len(reference_rows):
        raise AssertionError("reference row count divergence")
    return {"oracle_sha": ORACLE_SHA, "historical_variant_rows": count,
            "live_target_action_rows": target_count, "divergences": 0,
            "parameters": asdict(params), "final": final,
            "execution_proxy": "minute-BBO T+1; funding excluded",
            "live_target_parity": "same profile, same simulated inventory; not actual live execution PnL"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True)
    parser.add_argument("--cutoff", help="inclusive UTC minute, e.g. 2026-10-06T14:18:00Z")
    parser.add_argument("--reference-csv", help="frozen original shadow output")
    parser.add_argument("--output", required=True, help="new offline JSON report")
    args = parser.parse_args()
    with open(args.csv, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if args.cutoff:
        cutoff = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00")).timestamp()
        rows = [row for row in rows if float(row["minute_ts"]) <= cutoff]
    reference = None
    if args.reference_csv:
        with open(args.reference_csv, newline="") as fh:
            reference = list(csv.DictReader(fh))
    # Preserve source order exactly, including gaps; never shift by row count.
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    report = {"source_csv": str(Path(args.csv).resolve()),
              "source_sha256": hashlib.sha256(Path(args.csv).read_bytes()).hexdigest(),
              "selected_input_sha256": hashlib.sha256(canonical).hexdigest(),
              "minutes": len(rows), "first_utc": rows[0]["time_utc"], "last_utc": rows[-1]["time_utc"],
              "shadow_profile": check_parity(rows, reference_rows=reference),
              "canary_profile": check_parity(rows, params=CANARY_PARAMS)}
    output = Path(args.output)
    if output.exists():
        raise SystemExit("refusing to overwrite an existing parity report")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(output.resolve()), "minutes": len(rows),
                      "shadow_divergences": report["shadow_profile"]["divergences"],
                      "canary_divergences": report["canary_profile"]["divergences"],
                      "shadow_final": report["shadow_profile"]["final"]}, indent=2))


if __name__ == "__main__":
    main()
