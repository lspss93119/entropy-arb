"""Walk-forward rolling-window statistics and signal gates.

This module contains no venue or asyncio code. It consumes completed recorder
minute rows and produces immutable snapshots/signals for the engine.
"""
from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Mapping, Optional

from .config import RollingConf


@dataclass(frozen=True)
class RollingSnapshot:
    block_start_ts: float
    valid: bool
    mean_bps: Optional[float]
    std_bps: Optional[float]
    valid_minutes: int
    coverage_pct: float
    reason: str


@dataclass(frozen=True)
class RollingSignal:
    direction: str
    reason: str
    z: Optional[float]
    snapshot_ts: float
    mean_bps: Optional[float]
    std_bps: Optional[float]
    coverage_pct: float
    valid_minutes: int


class RollingWindow:
    """Store completed minute closes and calculate strictly walk-forward gates."""

    def __init__(self, config: RollingConf) -> None:
        self.config = config
        self._points: dict[float, float] = {}
        self._snapshots: dict[float, RollingSnapshot] = {}

    @property
    def points(self) -> tuple[tuple[float, float], ...]:
        return tuple(sorted(self._points.items()))

    @property
    def expected_minutes(self) -> int:
        return max(1, int(math.ceil(self.config.window_hours * 60.0)))

    @property
    def min_valid_minutes(self) -> int:
        return max(1, int(math.ceil(
            self.expected_minutes * self.config.min_coverage_pct / 100.0)))

    def ingest_row(self, row: Mapping[str, object]) -> bool:
        """Add one completed recorder row; return false for unusable rows."""
        try:
            minute_ts = float(row["minute_ts"])
            samples = int(float(row.get("samples", 0)))
            premium = float(row["premium_close_bps"])
        except (KeyError, TypeError, ValueError):
            return False
        if (not math.isfinite(minute_ts) or not math.isfinite(premium)
                or samples <= 0):
            return False
        self._points[minute_ts] = premium
        self._snapshots.clear()
        return True

    def load_csv(self, path: str) -> int:
        """Seed from an existing recorder CSV, returning accepted row count."""
        if not path or not os.path.exists(path):
            return 0
        accepted = 0
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                accepted += int(self.ingest_row(row))
        return accepted

    def _block_start(self, now: float) -> float:
        interval = self.config.update_minutes * 60
        return math.floor(float(now) / interval) * interval

    def snapshot_for(self, block_start_ts: float) -> RollingSnapshot:
        """Calculate the snapshot from rows strictly before this block."""
        block_start_ts = float(block_start_ts)
        cached = self._snapshots.get(block_start_ts)
        if cached is not None:
            return cached
        start = block_start_ts - self.config.window_hours * 3600.0
        values = [value for ts, value in self._points.items()
                  if start <= ts < block_start_ts and math.isfinite(value)]
        valid_minutes = len(values)
        coverage_pct = min(100.0, valid_minutes / self.expected_minutes * 100.0)
        gap_count = max(self.expected_minutes - valid_minutes, 0)
        if valid_minutes < self.min_valid_minutes:
            snapshot = RollingSnapshot(
                block_start_ts, False, None, None, valid_minutes,
                coverage_pct, f"insufficient coverage ({gap_count} missing)")
        else:
            center = mean(values)
            dispersion = pstdev(values)
            if not math.isfinite(dispersion) or dispersion <= 0.0:
                snapshot = RollingSnapshot(
                    block_start_ts, False, None, None, valid_minutes,
                    coverage_pct, "zero standard deviation")
            else:
                snapshot = RollingSnapshot(
                    block_start_ts, True, center, dispersion, valid_minutes,
                    coverage_pct, "quality ok")
        self._snapshots[block_start_ts] = snapshot
        return snapshot

    @staticmethod
    def _reverse(direction: str) -> str:
        if direction == "sell_entropy":
            return "buy_entropy"
        if direction == "buy_entropy":
            return "sell_entropy"
        raise ValueError(f"unknown rolling direction: {direction}")

    @staticmethod
    def _spreads_ok(entropy_spread_bps: Optional[float],
                    hedge_spread_bps: Optional[float],
                    max_spread_bps: float) -> bool:
        return (entropy_spread_bps is not None
                and hedge_spread_bps is not None
                and math.isfinite(float(entropy_spread_bps))
                and math.isfinite(float(hedge_spread_bps))
                and entropy_spread_bps <= max_spread_bps
                and hedge_spread_bps <= max_spread_bps)

    def entry_signal(self, premium_bps: float, now: float,
                     entropy_spread_bps: Optional[float],
                     hedge_spread_bps: Optional[float]
                     ) -> Optional[RollingSignal]:
        snapshot = self.snapshot_for(self._block_start(now))
        if not snapshot.valid:
            return None
        assert snapshot.mean_bps is not None and snapshot.std_bps is not None
        if not self._spreads_ok(entropy_spread_bps, hedge_spread_bps,
                                self.config.max_spread_bps):
            return None
        z = (float(premium_bps) - snapshot.mean_bps) / snapshot.std_bps
        if (not math.isfinite(z) or abs(z) < self.config.entry_z
                or abs(float(premium_bps) - snapshot.mean_bps)
                < self.config.min_reversion_bps):
            return None
        return RollingSignal(
            direction="sell_entropy" if z > 0 else "buy_entropy",
            reason="entry", z=z, snapshot_ts=snapshot.block_start_ts,
            mean_bps=snapshot.mean_bps, std_bps=snapshot.std_bps,
            coverage_pct=snapshot.coverage_pct,
            valid_minutes=snapshot.valid_minutes)

    def exit_signal(self, premium_bps: float, now: float,
                    entropy_spread_bps: Optional[float],
                    hedge_spread_bps: Optional[float], entry_ts: float,
                    direction: str) -> Optional[RollingSignal]:
        reverse = self._reverse(direction)
        if float(now) - float(entry_ts) >= self.config.timeout_hours * 3600.0:
            return RollingSignal(reverse, "timeout", None,
                                 self._block_start(now), None, None,
                                 0.0, 0)
        snapshot = self.snapshot_for(self._block_start(now))
        if not snapshot.valid:
            return None
        assert snapshot.mean_bps is not None and snapshot.std_bps is not None
        if not self._spreads_ok(entropy_spread_bps, hedge_spread_bps,
                                self.config.max_spread_bps):
            return None
        z = (float(premium_bps) - snapshot.mean_bps) / snapshot.std_bps
        if not math.isfinite(z) or abs(z) > self.config.exit_z:
            return None
        return RollingSignal(
            direction=reverse, reason="exit_z", z=z,
            snapshot_ts=snapshot.block_start_ts,
            mean_bps=snapshot.mean_bps, std_bps=snapshot.std_bps,
            coverage_pct=snapshot.coverage_pct,
            valid_minutes=snapshot.valid_minutes)
