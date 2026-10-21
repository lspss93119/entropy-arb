"""Optional Legacy executable-room rolling strategy.

Completely independent of the fixed-threshold strategy.  No orders are sent
here.  Call refresh() before scanning for trades; invalid windows fail closed.
"""
from __future__ import annotations

import csv
import io
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
    rows = list(rows)
    start = effective_at - WINDOW_SECONDS

    def invalid(reason):
        return _invalid(effective_at, start, 0, 0.0, reason)

    # Validate timestamps before selecting the window.  Otherwise a malformed
    # row can raise while being compared, or silently disappear from the
    # causal window.
    for row in rows:
        try:
            timestamp = row["ts"]
            if (isinstance(timestamp, bool)
                    or not isinstance(timestamp, (int, float))
                    or not math.isfinite(timestamp)
                    or int(timestamp) != timestamp):
                return invalid("invalid_timestamp")
        except (KeyError, TypeError, ValueError):
            return invalid("invalid_timestamp")

    selected = [r for r in rows if start <= r["ts"] < effective_at]
    count = len(selected)
    coverage = len({r["ts"] for r in selected}) / 480
    def bad(reason):
        return _invalid(effective_at, start, count, coverage, reason)

    if effective_at % 3600:
        return bad("effective_at_not_aligned")
    try:
        if any(r["market"] != market_key for r in selected):
            return bad("market_mismatch")
    except (KeyError, TypeError):
        return bad("market_mismatch")
    timestamps = [r["ts"] for r in selected]
    if any(t % 60 for t in timestamps):
        return bad("timestamp_not_aligned")
    if any(n > 1 for n in Counter(timestamps).values()):
        return bad("duplicate_timestamp")
    if set(timestamps) != set(range(start, effective_at, 60)):
        return bad("data_gap_or_insufficient_window")
    try:
        if any(
                isinstance(r["samples"], bool)
                or not isinstance(r["samples"], (int, float))
                or not math.isfinite(r["samples"])
                or r["samples"] < min_samples
                or int(r["samples"]) != r["samples"]
                or not all(math.isfinite(r[k]) for k in ("premium", "sell", "buy"))
                for r in selected):
            return bad("invalid_or_low_samples")
    except (KeyError, TypeError, ValueError):
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

    The dedicated CSV is normally scoped by its configured path.  If the
    recorder also provides a ``market_key`` or ``market`` column, it is
    validated row by row rather than trusted implicitly.
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
        self._snapshot_signature = None

    def _file_signature(self):
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def refresh(self, now):
        effective_at = int(now // 3600) * 3600
        signature = self._file_signature()
        if self.snapshot is not None and self.snapshot.effective_at == effective_at:
            # A valid snapshot is fixed for the effective hour.  An invalid
            # snapshot may be retried only after the source file changes; this
            # handles the recorder's short post-boundary flush interval without
            # rereading the complete CSV on every market-data update.
            if self.snapshot.valid or signature == self._snapshot_signature:
                return self.snapshot
        start = effective_at - WINDOW_SECONDS
        rows = []
        try:
            before_read = signature
            if before_read is None:
                raise OSError("CSV does not exist")
            with self.path.open(newline="") as fh:
                content = fh.read()
            after_read = self._file_signature()
            if before_read != after_read:
                raise ValueError("csv_changed_during_read")
            if content and not content.endswith("\n"):
                raise ValueError("incomplete_csv_tail")
            reader = csv.DictReader(io.StringIO(content), strict=True)
            fieldnames = reader.fieldnames or []
            identity_field = next(
                (name for name in ("market_key", "market") if name in fieldnames),
                None,
            )
            with_identity = identity_field is not None
            needed = {"minute_ts", "premium_close_bps",
                      "sell_edge_max_bps", "buy_edge_max_bps", "samples"}
            if not needed.issubset(fieldnames):
                raise ValueError("incompatible recorder schema")
            for raw in reader:
                if raw.get(None) is not None or any(
                        raw.get(name) is None for name in needed):
                    raise ValueError("malformed CSV row")
                ts = int(raw["minute_ts"])
                if start <= ts < effective_at:
                    rows.append({"ts": ts,
                                 "market": raw[identity_field]
                                 if with_identity else self.market_key,
                                 "premium": float(raw["premium_close_bps"]),
                                 "sell": float(raw["sell_edge_max_bps"]),
                                 "buy": float(raw["buy_edge_max_bps"]),
                                 "samples": float(raw["samples"])})
            snap = calculate_legacy(rows, effective_at, self.market_key,
                                    sell_quantile=self.sell_quantile,
                                    buy_quantile=self.buy_quantile,
                                    fees_bps=self.fees_bps,
                                    min_samples=self.min_samples)
        except csv.Error as exc:
            snap = _invalid(effective_at, start, len(rows),
                            len(rows) / 480, "csv_read_error:" + type(exc).__name__)
        except ValueError as exc:
            reason = str(exc)
            if reason not in {"incomplete_csv_tail", "csv_changed_during_read"}:
                reason = "csv_read_error:" + type(exc).__name__
            snap = _invalid(effective_at, start, len(rows),
                            len(rows) / 480, reason)
        except (OSError, TypeError, KeyError) as exc:
            snap = _invalid(effective_at, start, len(rows),
                            len(rows) / 480, "csv_read_error:" + type(exc).__name__)
        self.snapshot = snap
        # Keep the pre-read signature for a changed-during-read failure so a
        # subsequent call retries the now-stable file.  For all other cases,
        # this is the source version that produced the snapshot.
        self._snapshot_signature = self._file_signature()
        if snap.invalid_reason == "csv_changed_during_read":
            self._snapshot_signature = signature
        return snap
