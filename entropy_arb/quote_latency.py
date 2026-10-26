"""Pure data contracts for the opt-in public quote-latency recorder.

This module intentionally has no venue credentials, REST clients, Engine
imports, or order code.  It normalizes feed callbacks into bounded book
snapshots and preserves the distinction between a missing source timestamp
and a measured zero-age timestamp.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple

from .book import OrderBook


@dataclass(frozen=True)
class TimestampInfo:
    """Source timestamp after explicit unit normalization."""

    status: str  # present, missing, invalid
    raw_value: Any = None
    field: Optional[str] = None
    unit: Optional[str] = None
    source_timestamp_ns: Optional[int] = None

    def age_ms(self, received_wall_ns: int) -> Optional[float]:
        if self.source_timestamp_ns is None:
            return None
        return (received_wall_ns - self.source_timestamp_ns) / 1_000_000.0


@dataclass(frozen=True)
class FeedQuote:
    """One post-parse bounded order-book update."""

    venue: str
    kind: str
    received_wall_ns: int
    received_mono_ns: int
    connection_id: int
    source_timestamp: TimestampInfo
    bids: Tuple[Tuple[float, float], ...]
    asks: Tuple[Tuple[float, float], ...]

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def source_age_ms(self) -> Optional[float]:
        return self.source_timestamp.age_ms(self.received_wall_ns)


def _get_path(message: dict, path: Sequence[str]) -> tuple[bool, Any]:
    value: Any = message
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return False, None
        value = value[key]
    return True, value


def _timestamp_candidates(message: dict, venue: str):
    if venue == "entropy":
        return (
            ("data.time", ("data", "time")),
            ("data.timestamp", ("data", "timestamp")),
            ("timestamp", ("timestamp",)),
        )
    # RH order_book frames currently carry nonce/levels rather than a source
    # clock.  Keep only explicit timestamp-like fields; nonce is never a time.
    return (
        ("timestamp", ("timestamp",)),
        ("time", ("time",)),
        ("server_time", ("server_time",)),
        ("order_book.timestamp", ("order_book", "timestamp")),
        ("order_book.time", ("order_book", "time")),
        ("order_book.server_time", ("order_book", "server_time")),
    )


def _timestamp_unit(value: Decimal) -> tuple[str, Decimal]:
    magnitude = abs(value)
    if magnitude >= Decimal("1e18"):
        return "nanoseconds", Decimal("1")
    if magnitude >= Decimal("1e15"):
        return "microseconds", Decimal("1000")
    if magnitude >= Decimal("1e12"):
        return "milliseconds", Decimal("1000000")
    return "seconds", Decimal("1000000000")


def extract_source_timestamp(message: dict, venue: str) -> TimestampInfo:
    """Extract only an explicit source timestamp from a raw public frame."""
    for field, path in _timestamp_candidates(message, venue):
        present, raw = _get_path(message, path)
        if not present:
            continue
        if isinstance(raw, bool):
            return TimestampInfo("invalid", raw, field)
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return TimestampInfo("invalid", raw, field)
        if not value.is_finite() or value < 0:
            return TimestampInfo("invalid", raw, field)
        unit, multiplier = _timestamp_unit(value)
        return TimestampInfo("present", raw, field, unit,
                             int((value * multiplier).to_integral_value(
                                 rounding=ROUND_HALF_UP)))
    return TimestampInfo("missing")


def _bounded_levels(levels, depth_levels: int, *, reverse: bool):
    rows = []
    for price, size in levels:
        try:
            price, size = float(price), float(size)
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(price) and math.isfinite(size)):
            continue
        if price > 0 and size > 0:
            rows.append((price, size))
    rows.sort(key=lambda row: row[0], reverse=reverse)
    return tuple(rows[:depth_levels])


def normalize_feed_update(venue: str, kind: str, message: dict,
                          received_wall_ns: int, received_mono_ns: int,
                          connection_id: int, book: OrderBook,
                          depth_levels: int = 5) -> FeedQuote:
    """Capture bounded post-parse depth without copying the full book."""
    if depth_levels <= 0:
        raise ValueError("depth_levels must be positive")
    return FeedQuote(
        venue=venue,
        kind=kind,
        received_wall_ns=int(received_wall_ns),
        received_mono_ns=int(received_mono_ns),
        connection_id=int(connection_id),
        source_timestamp=extract_source_timestamp(message, venue),
        bids=_bounded_levels(book.bids.items(), depth_levels, reverse=True),
        asks=_bounded_levels(book.asks.items(), depth_levels, reverse=False),
    )


@dataclass(frozen=True)
class EdgeObservation:
    sell_edge_bps: Optional[float]
    buy_edge_bps: Optional[float]
    mid_premium_bps: Optional[float]
    pair_skew_ms: Optional[float]
    eligible: bool
    entropy_source_age_ms: Optional[float]
    hedge_source_age_ms: Optional[float]


def executable_edges(entropy: FeedQuote, hedge: FeedQuote,
                     max_pair_skew_ms: float = 250.0) -> EdgeObservation:
    """Use the same executable edge definitions as MinuteRecorder."""
    skew = abs(entropy.received_wall_ns - hedge.received_wall_ns) / 1e6
    if None in (entropy.best_bid, entropy.best_ask,
                hedge.best_bid, hedge.best_ask):
        return EdgeObservation(None, None, None, skew, False,
                               entropy.source_age_ms, hedge.source_age_ms)
    e_bid, e_ask = entropy.best_bid, entropy.best_ask
    h_bid, h_ask = hedge.best_bid, hedge.best_ask
    assert e_bid is not None and e_ask is not None
    assert h_bid is not None and h_ask is not None
    e_mid = (e_bid + e_ask) / 2.0
    h_mid = (h_bid + h_ask) / 2.0
    sell = (e_bid / h_ask - 1.0) * 1e4
    buy = (h_bid / e_ask - 1.0) * 1e4
    mid = (e_mid / h_mid - 1.0) * 1e4
    return EdgeObservation(
        sell_edge_bps=sell,
        buy_edge_bps=buy,
        mid_premium_bps=mid,
        pair_skew_ms=skew,
        eligible=math.isfinite(skew) and skew <= max_pair_skew_ms,
        entropy_source_age_ms=entropy.source_age_ms,
        hedge_source_age_ms=hedge.source_age_ms,
    )


def _parameter_fields(params: object) -> dict:
    if params is None:
        return {
            "parameter_valid": False,
            "parameter_invalid_reason": "missing_snapshot",
            "effective_at": None,
            "window_start": None,
            "window_end": None,
            "sample_count": None,
            "coverage": None,
            "midline_bps": None,
            "upper_bps": None,
            "lower_bps": None,
        }
    valid = bool(getattr(params, "valid", False))
    return {
        "parameter_valid": valid,
        "parameter_invalid_reason": getattr(params, "invalid_reason", None),
        "effective_at": getattr(params, "effective_at", None),
        "window_start": getattr(params, "window_start", None),
        "window_end": getattr(params, "window_end", None),
        "sample_count": getattr(params, "sample_count", None),
        "coverage": getattr(params, "coverage", None),
        "midline_bps": getattr(params, "midline_bps", None),
        "upper_bps": getattr(params, "upper_bps", None),
        "lower_bps": getattr(params, "lower_bps", None),
    }


class EdgeDecayTracker:
    """Track rising candidate edges and first observable post-signal states."""

    HORIZONS_MS = (50, 100, 250, 500, 1000)

    def __init__(self, *, pair_max_skew_ms: float = 250.0,
                 max_pending_signals: int = 512) -> None:
        if pair_max_skew_ms < 0:
            raise ValueError("pair_max_skew_ms must be non-negative")
        if max_pending_signals <= 0:
            raise ValueError("max_pending_signals must be positive")
        self.pair_max_skew_ms = pair_max_skew_ms
        self.max_pending_signals = max_pending_signals
        self._latest: dict[str, FeedQuote] = {}
        self._active: set[str] = set()
        self._pending: dict[str, dict] = {}
        self._next_signal = 1
        self.dropped_signals = 0

    def _pair(self) -> Optional[tuple[FeedQuote, FeedQuote]]:
        entropy = self._latest.get("entropy")
        hedge = self._latest.get("lighter-rh")
        if entropy is None or hedge is None:
            return None
        return entropy, hedge

    @staticmethod
    def _thresholds(params: object) -> Optional[tuple[float, float]]:
        if params is None or not bool(getattr(params, "valid", False)):
            return None
        try:
            return (float(getattr(params, "midline_bps"))
                    + float(getattr(params, "upper_bps")),
                    float(getattr(params, "lower_bps"))
                    - float(getattr(params, "midline_bps")))
        except (TypeError, ValueError):
            return None

    def _observation(self) -> Optional[EdgeObservation]:
        pair = self._pair()
        if pair is None:
            return None
        return executable_edges(pair[0], pair[1], self.pair_max_skew_ms)

    def _base_record(self, obs: Optional[EdgeObservation], params: object) -> dict:
        row = _parameter_fields(params)
        if obs is None:
            row.update({
                "sell_edge_bps": None, "buy_edge_bps": None,
                "mid_premium_bps": None, "pair_skew_ms": None,
                "eligible": False, "entropy_source_age_ms": None,
                "hedge_source_age_ms": None,
            })
        else:
            row.update({
                "sell_edge_bps": obs.sell_edge_bps,
                "buy_edge_bps": obs.buy_edge_bps,
                "mid_premium_bps": obs.mid_premium_bps,
                "pair_skew_ms": obs.pair_skew_ms,
                "eligible": obs.eligible,
                "entropy_source_age_ms": obs.entropy_source_age_ms,
                "hedge_source_age_ms": obs.hedge_source_age_ms,
            })
        return row

    def _collect_due(self, now_wall_ns: int, params: object,
                     *, final: bool = False) -> list[dict]:
        rows = []
        pair = self._pair()
        obs = self._observation()
        for signal_id, pending in list(self._pending.items()):
            remaining = []
            for target_ms, due_ns in pending["remaining"]:
                if not final and due_ns > now_wall_ns:
                    remaining.append((target_ms, due_ns))
                    continue
                latest_ns = (max(q.received_wall_ns for q in pair)
                             if pair is not None else None)
                observed = pair is not None and latest_ns is not None and latest_ns >= due_ns
                sampling_lag_ms = ((latest_ns - due_ns) / 1e6 if observed else None)
                # A later update cannot be represented as an exact earlier sample.
                within_tolerance = (observed and sampling_lag_ms <= 50.0)
                row = {
                    "record_type": "decay",
                    "signal_id": signal_id,
                    "direction": pending["direction"],
                    "target_ms": target_ms,
                    "signal_wall_ns": pending["signal_wall_ns"],
                    "observed_wall_ns": latest_ns if observed else None,
                    "sampling_lag_ms": sampling_lag_ms,
                    "observed_after_ms": (
                        (latest_ns - pending["signal_wall_ns"]) / 1e6
                        if observed else None),
                    "status": ("observed" if within_tolerance else
                               "late_quote_outside_tolerance" if observed else
                               "not_observed_before_capture_end"
                               if final else "missing_quote_after_horizon"),
                }
                row.update(self._base_record(obs if within_tolerance else None,
                                              params if within_tolerance else None))
                rows.append(row)
            if remaining:
                pending["remaining"] = remaining
            else:
                del self._pending[signal_id]
        return rows

    def ingest(self, quote: FeedQuote, params: object) -> list[dict]:
        if quote.venue not in {"entropy", "lighter-rh"}:
            raise ValueError(f"unexpected quote venue {quote.venue!r}")
        self._latest[quote.venue] = quote
        rows = []
        thresholds = self._thresholds(params)
        obs = self._observation()
        if thresholds is None or obs is None or not obs.eligible:
            self._active.clear()
        else:
            edges = {"sell_entropy": obs.sell_edge_bps,
                     "buy_entropy": obs.buy_edge_bps}
            for direction, edge in edges.items():
                threshold = thresholds[0] if direction == "sell_entropy" else thresholds[1]
                qualifies = edge is not None and edge >= threshold
                if not qualifies:
                    self._active.discard(direction)
                    continue
                if direction in self._active:
                    continue
                self._active.add(direction)
                if len(self._pending) >= self.max_pending_signals:
                    self.dropped_signals += 1
                    continue
                signal_id = f"signal-{self._next_signal:06d}"
                self._next_signal += 1
                signal_wall_ns = max(
                    self._latest["entropy"].received_wall_ns,
                    self._latest["lighter-rh"].received_wall_ns)
                row = {
                    "record_type": "signal", "signal_id": signal_id,
                    "direction": direction,
                    "signal_wall_ns": signal_wall_ns,
                    "threshold_bps": threshold,
                    "candidate": True,
                }
                row.update(self._base_record(obs, params))
                rows.append(row)
                self._pending[signal_id] = {
                    "direction": direction,
                    "signal_wall_ns": signal_wall_ns,
                    "remaining": [(target, signal_wall_ns + target * 1_000_000)
                                   for target in self.HORIZONS_MS],
                }
        rows.extend(self._collect_due(quote.received_wall_ns, params))
        return rows

    def flush(self, now_wall_ns: int, *, params: object = None,
              final: bool = False) -> list[dict]:
        return self._collect_due(now_wall_ns, params, final=final)

    @property
    def pending_count(self) -> int:
        return sum(len(item["remaining"]) for item in self._pending.values())


class BoundedJsonlWriter:
    """Append JSONL while rotating and expiring only this writer's files."""

    def __init__(self, directory: str | Path, prefix: str, *,
                 max_rows: int = 50_000, max_bytes: int = 64 * 1024 * 1024,
                 retention_hours: float = 24.0, max_files: int = 24,
                 preserve_files: bool = False,
                 run_id: Optional[str] = None) -> None:
        if max_rows <= 0 or max_bytes <= 0 or max_files <= 0:
            raise ValueError("writer bounds must be positive")
        if retention_hours < 0:
            raise ValueError("retention_hours must be non-negative")
        self.directory = Path(directory)
        self.prefix = prefix
        self.max_rows = max_rows
        self.max_bytes = max_bytes
        self.retention_seconds = retention_hours * 3600.0
        self.max_files = max_files
        self.preserve_files = bool(preserve_files)
        self.run_id = run_id or datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ")
        self._fh = None
        self._path: Optional[Path] = None
        self._rows = 0
        self._bytes = 0
        self._sequence = 0
        self._stats = {"rows_written": 0, "rotations": 0,
                       "files_expired": 0, "write_errors": 0}
        if not self.preserve_files:
            self._cleanup()

    def _matching_files(self) -> list[Path]:
        return sorted(self.directory.glob(f"{self.prefix}-*.jsonl"),
                      key=lambda path: path.stat().st_mtime_ns)

    def _cleanup(self) -> None:
        if not self.directory.exists():
            return
        now = time.time()
        files = self._matching_files()
        for path in files:
            try:
                if self.retention_seconds and now - path.stat().st_mtime > self.retention_seconds:
                    path.unlink()
                    self._stats["files_expired"] += 1
            except OSError:
                continue
        files = self._matching_files()
        while len(files) > self.max_files:
            path = files.pop(0)
            try:
                path.unlink()
            except OSError:
                continue
            self._stats["files_expired"] += 1

    def _open_next(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._sequence += 1
        self._path = self.directory / (
            f"{self.prefix}-{self.run_id}-{self._sequence:04d}.jsonl")
        self._fh = self._path.open("a", encoding="utf-8")
        self._rows = 0
        self._bytes = self._path.stat().st_size

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded = line.encode("utf-8")
        if self._fh is None:
            self._open_next()
        if self._rows and (self._rows >= self.max_rows
                           or self._bytes + len(encoded) > self.max_bytes):
            self._fh.close()
            self._stats["rotations"] += 1
            self._open_next()
        try:
            self._fh.write(line)
            self._fh.flush()
        except OSError:
            self._stats["write_errors"] += 1
            raise
        self._rows += 1
        self._bytes += len(encoded)
        self._stats["rows_written"] += 1
        if not self.preserve_files:
            self._cleanup()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    @property
    def stats(self) -> dict:
        return dict(self._stats)
