from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque

OBSERVATION_INTERVAL_SEC = 1.0
MIN_COVERAGE_RATIO = 0.90
DISCONTINUITY_RESET_SEC = 30.0


@dataclass(frozen=True)
class StrategyState:
    ready: bool
    center_bps: float | None
    upper_bps: float
    lower_bps: float
    window_minutes: int | None = None
    warmup_span_sec: float | None = None
    coverage_ratio: float | None = None


class StableBasisStrategy:
    name = "stable_basis"
    requires_observations = False

    def __init__(self, *, center_bps: float, upper_bps: float, lower_bps: float):
        self._state = StrategyState(
            ready=True,
            center_bps=center_bps,
            upper_bps=upper_bps,
            lower_bps=lower_bps,
        )

    def update(self, timestamp: float, premium_bps: float) -> None:
        return None

    def state(self) -> StrategyState:
        return self._state


class DriftingBasisStrategy:
    name = "drifting_basis"
    requires_observations = True

    def __init__(self, *, window_minutes: int, upper_bps: float, lower_bps: float):
        self.window_minutes = window_minutes
        self.window_sec = float(window_minutes * 60)
        self.upper_bps = upper_bps
        self.lower_bps = lower_bps
        self._samples: Deque[tuple[float, float]] = deque()
        self._segment_start_ts: float | None = None
        self._last_valid_ts: float | None = None

    def update(self, timestamp: float, premium_bps: float) -> None:
        if not (math.isfinite(timestamp) and math.isfinite(premium_bps)):
            return

        if (
            self._last_valid_ts is not None
            and timestamp - self._last_valid_ts > DISCONTINUITY_RESET_SEC
        ):
            self._samples.clear()
            self._segment_start_ts = None

        if self._segment_start_ts is None:
            self._segment_start_ts = timestamp

        self._last_valid_ts = timestamp
        self._samples.append((timestamp, premium_bps))

        cutoff = timestamp - self.window_sec
        while self._samples and self._samples[0][0] <= cutoff:
            self._samples.popleft()

    def state(self) -> StrategyState:
        if self._last_valid_ts is None or self._segment_start_ts is None:
            return StrategyState(
                ready=False,
                center_bps=None,
                upper_bps=self.upper_bps,
                lower_bps=self.lower_bps,
                window_minutes=self.window_minutes,
                warmup_span_sec=0.0,
                coverage_ratio=0.0,
            )

        span = max(self._last_valid_ts - self._segment_start_ts, 0.0)
        expected = self.window_sec / OBSERVATION_INTERVAL_SEC
        coverage = min(len(self._samples) / expected, 1.0)
        ready = span >= self.window_sec and coverage >= MIN_COVERAGE_RATIO
        center = (
            statistics.median(value for _, value in self._samples) if ready else None
        )
        return StrategyState(
            ready=ready,
            center_bps=center,
            upper_bps=self.upper_bps,
            lower_bps=self.lower_bps,
            window_minutes=self.window_minutes,
            warmup_span_sec=span,
            coverage_ratio=coverage,
        )
