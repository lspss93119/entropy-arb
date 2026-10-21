"""Optional Legacy executable-room rolling strategy.

Completely independent of the fixed-threshold strategy.  No orders are sent
here.  Call refresh() before scanning for trades; invalid windows fail closed.
"""
from __future__ import annotations

import csv
import math
from collections import Counter
from pathlib import Path

from .rolling import RollingParameters, _invalid, _quantile, WINDOW_SECONDS


def _band(values, quantile):
    # Preserve tools/analyze.py half-bps rounding and minimum one bps.
    return max(round(_quantile(values, quantile) * 2) / 2, 1.0)


def calculate_legacy(rows, effective_at, market_key, *, sell_quantile=0.80,
                     buy_quantile=0.50, fees_bps=0.0, min_samples=10):
    """Causal completed-minute [effective_at - 8h, effective_at) snapshot."""
    start = effective_at - WINDOW_SECONDS
    selected = [r for r in rows if start <= r["ts"] < effective_at]
    count = len(selected)
    coverage = len({r["ts"] for r in selected}) / 480
    def bad(reason):
        return _invalid(effective_at, start, count, coverage, reason)

    if effective_at % 3600:
        return bad("effective_at_not_aligned")
    if any(r["market"] != market_key for r in selected):
        return bad("market_mismatch")
    timestamps = [r["ts"] for r in selected]
    if any(t % 60 for t in timestamps):
        return bad("timestamp_not_aligned")
    if any(n > 1 for n in Counter(timestamps).values()):
        return bad("duplicate_timestamp")
    if set(timestamps) != set(range(start, effective_at, 60)):
        return bad("data_gap_or_insufficient_window")
    if any(r["samples"] < min_samples or not all(
            math.isfinite(r[k]) for k in ("premium", "sell", "buy"))
           for r in selected):
        return bad("invalid_or_low_samples")

    midline = round(_quantile([r["premium"] for r in selected], 0.5), 1)
    sell_room = [r["sell"] - midline - fees_bps for r in selected]
    buy_room = [r["buy"] + midline - fees_bps for r in selected]
    upper = _band(sell_room, sell_quantile)
    lower = _band(buy_room, buy_quantile)
    if not all(math.isfinite(x) for x in (midline, upper, lower)):
        return bad("invalid_parameters")
    return RollingParameters(effective_at, start, effective_at, count, coverage,
                             midline, upper, lower, True, None)


class LegacyRollingStrategy:
    """Read-only minute CSV parameter provider scoped to exactly one pair.

    The dedicated CSV must be written exclusively by the selected pair;
    the CSV schema itself has no market identity.
    """
    def __init__(self, *, csv_path, market_key, sell_quantile=0.80,
                 buy_quantile=0.50, fees_bps=0.0, min_samples=10):
        if not market_key or not csv_path:
            raise ValueError("dedicated CSV path and market key are required")
        if not (0 < sell_quantile < 1 and 0 < buy_quantile < 1):
            raise ValueError("quantiles must be inside (0,1)")
        self.path = Path(csv_path)
        self.market_key = market_key
        self.sell_quantile = sell_quantile
        self.buy_quantile = buy_quantile
        self.fees_bps = fees_bps
        self.min_samples = min_samples
        self.snapshot = None

    def refresh(self, now):
        effective_at = int(now // 3600) * 3600
        if self.snapshot is not None and self.snapshot.effective_at == effective_at:
            return self.snapshot
        start = effective_at - WINDOW_SECONDS
        rows = []
        try:
            with self.path.open(newline="") as fh:
                reader = csv.DictReader(fh)
                needed = {"minute_ts", "premium_close_bps",
                          "sell_edge_max_bps", "buy_edge_max_bps", "samples"}
                if not needed.issubset(reader.fieldnames or []):
                    raise ValueError("incompatible recorder schema")
                for raw in reader:
                    ts = int(raw["minute_ts"])
                    if start <= ts < effective_at:
                        rows.append({"ts": ts, "market": self.market_key,
                                     "premium": float(raw["premium_close_bps"]),
                                     "sell": float(raw["sell_edge_max_bps"]),
                                     "buy": float(raw["buy_edge_max_bps"]),
                                     "samples": float(raw["samples"])})
            snap = calculate_legacy(rows, effective_at, self.market_key,
                                    sell_quantile=self.sell_quantile,
                                    buy_quantile=self.buy_quantile,
                                    fees_bps=self.fees_bps,
                                    min_samples=self.min_samples)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            snap = _invalid(effective_at, start, len(rows),
                            len(rows) / 480, "csv_read_error:" + type(exc).__name__)
        self.snapshot = snap
        return snap
