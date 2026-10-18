"""Causal minute-level threshold *signal* shadow, not an execution backtest.

CSV is the existing MinuteRecorder schema. Never treat qualifying minute
highs as fills, or this report as realized PnL.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


def read_minutes(path: str) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        for raw in csv.DictReader(f):
            try:
                t = datetime.fromisoformat(raw["time_utc"].replace("Z", "+00:00"))
                if t.tzinfo is None:
                    raise ValueError("timezone missing")
                t = t.astimezone(timezone.utc)
                close = float(raw["premium_close_bps"])
                sell = float(raw["sell_edge_max_bps"])
                buy = float(raw["buy_edge_max_bps"])
                samples = int(raw["samples"])
                if not all(map(math.isfinite, (close, sell, buy))) or samples < 1:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            rows.append({"time": t, "close": close, "sell": sell, "buy": buy})
    rows.sort(key=lambda r: r["time"])
    if len({r["time"] for r in rows}) != len(rows):
        raise ValueError("duplicate UTC minute; resolve sources before analysis")
    return rows


def shadow(rows: list[dict], train_count: int, windows=(60, 240),
           upper: float = 2.0, lower: float = 2.0) -> list[dict]:
    """All midlines use closed minutes strictly before the decision minute.

    Fixed reference is trained on the initial train_count rows, then frozen.
    Rolling windows require uninterrupted *clock minutes*, never simply N
    rows across gaps. No result is produced before sufficient history.
    """
    if train_count < 1 or train_count >= len(rows):
        raise ValueError("train_count must leave a nonempty evaluation period")
    if upper <= 0 or lower <= 0:
        raise ValueError("bands must be positive")
    fixed = statistics.median(r["close"] for r in rows[:train_count])
    history: deque = deque(maxlen=max(windows))
    out = []
    previous = None
    for i, r in enumerate(rows):
        t = r["time"]
        if previous is not None and (t - previous).total_seconds() != 60:
            history.clear()
        centers = {"fixed": fixed}
        for w in windows:
            if len(history) >= w:
                centers[f"rolling_{w}m"] = statistics.median(v for _, v in
                                                             list(history)[-w:])
        if i >= train_count:
            result = {"time_utc": t.isoformat(), "premium_close_bps": r["close"]}
            for name in ("fixed", *(f"rolling_{w}m" for w in windows)):
                center = centers.get(name)
                result[name + "_midline"] = center
                result[name + "_sell_signal"] = (
                    r["sell"] >= center + upper if center is not None else None)
                # buy_edge is hedge bid / entropy ask - 1, i.e. reverse premium.
                result[name + "_buy_signal"] = (
                    r["buy"] >= lower - center if center is not None else None)
            out.append(result)
        history.append((t, r["close"]))
        previous = t
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--csv", required=True)
    p.add_argument("--train-fraction", type=float, default=2 / 3)
    p.add_argument("--upper", type=float, default=2.0)
    p.add_argument("--lower", type=float, default=2.0)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    if not 0 < args.train_fraction < 1:
        p.error("train-fraction must be between 0 and 1")
    rows = read_minutes(args.csv)
    if len(rows) < 3:
        p.error("need at least three valid minutes")
    results = shadow(rows, max(1, min(len(rows)-1, int(len(rows)*args.train_fraction))),
                     upper=args.upper, lower=args.lower)
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    summary = {"input_rows": len(rows), "evaluation_rows": len(results),
               "train_rows": len(rows)-len(results), "models": {}}
    for name in ("fixed", "rolling_60m", "rolling_240m"):
        eligible = [r for r in results if r[name + "_midline"] is not None]
        summary["models"][name] = {
            "eligible_minutes": len(eligible),
            "sell_signal_minutes": sum(r[name+"_sell_signal"] for r in eligible),
            "buy_signal_minutes": sum(r[name+"_buy_signal"] for r in eligible),
        }
    print(json.dumps(summary, indent=2))
    print("SIGNALS ONLY: no inventory, fills, MTM, trading PnL, or Canary conclusion.")


if __name__ == "__main__":
    main()
