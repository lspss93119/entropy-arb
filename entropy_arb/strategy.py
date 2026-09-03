from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable

from .config import (
    DEFAULT_CENTER_MAX_LATEST_SAMPLE_AGE_SEC,
    DEFAULT_CENTER_LAST_VALID_MAX_AGE_HOURS,
    DEFAULT_CENTER_MIN_COVERAGE_RATIO,
    DEFAULT_CENTER_MIN_SAMPLES,
    StrategyConf,
)

OBSERVATION_INTERVAL_SEC = 1.0
MIN_COVERAGE_RATIO = 0.90
DISCONTINUITY_RESET_SEC = 30.0
COVERAGE_BUCKET_SEC = 60.0


@dataclass(frozen=True)
class StrategyState:
    ready: bool
    center_bps: float | None
    upper_bps: float
    lower_bps: float
    window_minutes: int | None = None
    warmup_span_sec: float | None = None
    coverage_ratio: float | None = None
    center_source: str | None = None
    latest_sample_age_sec: float | None = None


@dataclass(frozen=True)
class RollingCenterSnapshot:
    center_bps: float
    calculated_at: float
    window_start: float
    window_end: float
    coverage_ratio: float
    sample_count: int


@dataclass(frozen=True)
class RollingCenterUpdate:
    """A valid center recalculation, for startup/runtime observability."""

    old_center_bps: float
    new_center_bps: float
    window_sec: float
    samples: int
    range_min_bps: float
    range_max_bps: float
    observed_at: float
    coverage_ratio: float = 0.0
    window_start: float = 0.0
    window_end: float = 0.0
    latest_sample_age_sec: float = 0.0


class StableBasisStrategy:
    name = "stable_basis"
    requires_observations = False

    def __init__(
        self,
        *,
        center_bps: float,
        upper_bps: float,
        lower_bps: float,
        center_mode: str = "fixed",
        center_window_hours: float = 12.0,
        center_update_minutes: int = 60,
        center_min_coverage_ratio: float = DEFAULT_CENTER_MIN_COVERAGE_RATIO,
        center_min_samples: int = DEFAULT_CENTER_MIN_SAMPLES,
        center_last_valid_max_age_hours: float = (
            DEFAULT_CENTER_LAST_VALID_MAX_AGE_HOURS
        ),
        center_max_latest_sample_age_sec: float = (
            DEFAULT_CENTER_MAX_LATEST_SAMPLE_AGE_SEC
        ),
    ):
        if center_mode not in ("fixed", "rolling"):
            raise ValueError("center_mode must be 'fixed' or 'rolling'")
        if not math.isfinite(center_bps):
            raise ValueError("center_bps must be finite")
        if not math.isfinite(center_window_hours) or center_window_hours <= 0:
            raise ValueError("center_window_hours must be > 0")
        if not isinstance(center_update_minutes, int) or center_update_minutes <= 0:
            raise ValueError("center_update_minutes must be a positive integer")
        if (
            not math.isfinite(center_min_coverage_ratio)
            or not 0.0 < center_min_coverage_ratio <= 1.0
        ):
            raise ValueError("center_min_coverage_ratio must be in (0, 1]")
        if (
            not isinstance(center_min_samples, int)
            or isinstance(center_min_samples, bool)
            or center_min_samples <= 0
        ):
            raise ValueError("center_min_samples must be a positive integer")
        if (
            not math.isfinite(center_last_valid_max_age_hours)
            or center_last_valid_max_age_hours <= 0
        ):
            raise ValueError("center_last_valid_max_age_hours must be > 0")
        if (
            not math.isfinite(center_max_latest_sample_age_sec)
            or center_max_latest_sample_age_sec <= 0
        ):
            raise ValueError("center_max_latest_sample_age_sec must be > 0")

        self.center_mode = center_mode
        self.fixed_center_bps = center_bps
        self.center_window_hours = float(center_window_hours)
        self.center_update_minutes = center_update_minutes
        self.window_sec = self.center_window_hours * 3600.0
        self.update_sec = float(center_update_minutes * 60)
        self.requires_observations = center_mode == "rolling"
        self.center_min_coverage_ratio = float(center_min_coverage_ratio)
        self.center_min_samples = center_min_samples
        self.center_last_valid_max_age_hours = float(
            center_last_valid_max_age_hours
        )
        self.center_max_latest_sample_age_sec = float(
            center_max_latest_sample_age_sec
        )
        self.upper_bps = upper_bps
        self.lower_bps = lower_bps
        self._effective_center_bps = center_bps
        self._center_source = "fixed_fallback"
        self._rolling_samples: Deque[tuple[float, float]] = deque()
        self._rolling_ready = False
        self._next_update_ts: float | None = None
        self._last_valid_snapshot: RollingCenterSnapshot | None = None
        self._last_observation_ts: float | None = None

    def _window_samples(self, timestamp: float) -> list[tuple[float, float]]:
        cutoff = timestamp - self.window_sec
        return [
            (ts, value)
            for ts, value in self._rolling_samples
            if cutoff <= ts < timestamp
            and math.isfinite(ts)
            and math.isfinite(value)
        ]

    def _window_metrics(
        self, timestamp: float
    ) -> tuple[list[tuple[float, float]], float, float]:
        samples = self._window_samples(timestamp)
        if not samples:
            return [], 0.0, 0.0
        window_start = timestamp - self.window_sec
        bucket_count = max(1, math.ceil(self.window_sec / COVERAGE_BUCKET_SEC))
        covered: set[int] = set()
        for sample_ts, _ in samples:
            index = int((sample_ts - window_start) // COVERAGE_BUCKET_SEC)
            if 0 <= index < bucket_count:
                covered.add(index)
        covered_seconds = sum(
            min(COVERAGE_BUCKET_SEC,
                max(0.0, self.window_sec - index * COVERAGE_BUCKET_SEC))
            for index in covered
        )
        coverage = min(covered_seconds / self.window_sec, 1.0)
        span = max(ts for ts, _ in samples) - min(ts for ts, _ in samples)
        return samples, coverage, span

    def _valid_window_values(
        self, timestamp: float
    ) -> list[tuple[float, float]] | None:
        samples, coverage, _ = self._window_metrics(timestamp)
        if len(samples) < self.center_min_samples:
            return None
        if coverage < self.center_min_coverage_ratio:
            return None
        if self._latest_sample_age(samples, timestamp) > (
            self.center_max_latest_sample_age_sec
        ):
            return None
        return samples

    @staticmethod
    def _latest_sample_age(
        samples: list[tuple[float, float]], timestamp: float
    ) -> float:
        return max(0.0, timestamp - max(ts for ts, _ in samples))

    def latest_sample_age_sec(self, timestamp: float) -> float | None:
        """Return the age of the newest valid sample in the causal window."""
        if not math.isfinite(timestamp):
            return None
        samples = self._window_samples(timestamp)
        if not samples:
            return None
        return self._latest_sample_age(samples, timestamp)

    def _prune(self, timestamp: float) -> None:
        cutoff = timestamp - self.window_sec
        while self._rolling_samples and self._rolling_samples[0][0] < cutoff:
            self._rolling_samples.popleft()

    def bootstrap(
        self,
        observations: Iterable[tuple[float, float]],
        *,
        now: float,
    ) -> RollingCenterUpdate | None:
        """Seed a rolling center from bounded historical observations.

        ``observations`` are expected to be chronological, as returned by the
        SQLite history query.  Values at or after ``now`` are deliberately
        excluded so bootstrap has the same causal boundary as live updates.
        """
        if self.center_mode != "rolling":
            return None
        self._rolling_samples.clear()
        self._rolling_ready = self._center_source == "last_valid"
        if not self._usable_last_valid(now):
            self._rolling_ready = False
            self._effective_center_bps = self.fixed_center_bps
            self._center_source = "fixed_fallback"
        if not math.isfinite(now):
            self._next_update_ts = None
            return None

        valid = sorted(
            (float(timestamp), float(value))
            for timestamp, value in observations
            if math.isfinite(timestamp)
            and math.isfinite(value)
            and timestamp < now
        )
        self._rolling_samples.extend(valid)
        self._prune(now)
        self._last_observation_ts = now
        self._next_update_ts = now + self.update_sec
        samples = self._valid_window_values(now)
        if samples is None:
            return None
        values = [value for _, value in samples]
        new_center = statistics.median(values)
        return self._accept_fresh_center(new_center, values, now)

    def restore_last_valid(
        self, snapshot: RollingCenterSnapshot | None, *, now: float
    ) -> bool:
        """Restore a recent durable center for rolling-mode startup."""
        if self.center_mode != "rolling" or snapshot is None:
            return False
        if not self._snapshot_is_valid(snapshot, now):
            return False
        self._last_valid_snapshot = snapshot
        self._effective_center_bps = snapshot.center_bps
        self._center_source = "last_valid"
        self._rolling_ready = True
        return True

    def last_valid_snapshot(self) -> RollingCenterSnapshot | None:
        return self._last_valid_snapshot

    def _snapshot_is_valid(
        self, snapshot: RollingCenterSnapshot, now: float
    ) -> bool:
        values = (
            snapshot.center_bps,
            snapshot.calculated_at,
            snapshot.window_start,
            snapshot.window_end,
            snapshot.coverage_ratio,
        )
        return (
            math.isfinite(now)
            and all(math.isfinite(value) for value in values)
            and snapshot.sample_count > 0
            and 0.0 <= snapshot.coverage_ratio <= 1.0
            and snapshot.window_start <= snapshot.window_end
            and snapshot.calculated_at <= now
            and now - snapshot.calculated_at
            <= self.center_last_valid_max_age_hours * 3600.0
        )

    def _usable_last_valid(self, now: float) -> bool:
        return self._last_valid_snapshot is not None and self._snapshot_is_valid(
            self._last_valid_snapshot, now
        )

    def _accept_fresh_center(
        self, new_center: float, values: list[float], now: float
    ) -> RollingCenterUpdate:
        window_samples, coverage, _ = self._window_metrics(now)
        latest_age = self._latest_sample_age(window_samples, now)
        old_center = self._effective_center_bps
        self._effective_center_bps = new_center
        self._center_source = "fresh_rolling"
        self._rolling_ready = True
        snapshot = RollingCenterSnapshot(
            center_bps=new_center,
            calculated_at=now,
            window_start=now - self.window_sec,
            window_end=now,
            coverage_ratio=coverage,
            sample_count=len(values),
        )
        self._last_valid_snapshot = snapshot
        return RollingCenterUpdate(
            old_center_bps=old_center,
            new_center_bps=new_center,
            window_sec=self.window_sec,
            samples=len(values),
            range_min_bps=min(values),
            range_max_bps=max(values),
            observed_at=now,
            coverage_ratio=coverage,
            window_start=now - self.window_sec,
            window_end=now,
            latest_sample_age_sec=latest_age,
        )

    def update(
        self, timestamp: float, premium_bps: float
    ) -> RollingCenterUpdate | None:
        if self.center_mode != "rolling":
            return None
        if not (math.isfinite(timestamp) and math.isfinite(premium_bps)):
            return None

        self._rolling_samples.append((timestamp, premium_bps))
        self._prune(timestamp)
        self._last_observation_ts = timestamp
        if self._next_update_ts is None:
            self._next_update_ts = timestamp + self.update_sec
            return None
        if timestamp < self._next_update_ts:
            return None

        samples = self._valid_window_values(timestamp)
        self._next_update_ts = timestamp + self.update_sec
        if samples is None:
            if self._usable_last_valid(timestamp):
                assert self._last_valid_snapshot is not None
                self._effective_center_bps = self._last_valid_snapshot.center_bps
                self._center_source = "last_valid"
                self._rolling_ready = True
            else:
                self._effective_center_bps = self.fixed_center_bps
                self._center_source = "fixed_fallback"
                self._rolling_ready = False
            return None
        values = [value for _, value in samples]
        new_center = statistics.median(values)
        return self._accept_fresh_center(new_center, values, timestamp)

    def state(self) -> StrategyState:
        coverage = None
        latest_age = None
        if self.center_mode == "rolling" and self._last_observation_ts is not None:
            evaluation_ts = self._last_observation_ts
            coverage = self._window_metrics(evaluation_ts)[1]
            latest_age = self.latest_sample_age_sec(evaluation_ts)
        return StrategyState(
            ready=True,
            center_bps=self._effective_center_bps,
            upper_bps=self.upper_bps,
            lower_bps=self.lower_bps,
            coverage_ratio=coverage,
            center_source=self._center_source,
            latest_sample_age_sec=latest_age,
        )

    def rolling_history_summary(self, *, now: float) -> tuple[int, float]:
        samples = self._window_samples(now)
        if not samples:
            return 0, 0.0
        return (
            len(samples),
            max(ts for ts, _ in samples) - min(ts for ts, _ in samples),
        )

    def rolling_coverage_summary(self, *, now: float) -> tuple[int, float, float]:
        samples, coverage, span = self._window_metrics(now)
        return len(samples), span, coverage


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
                center_source="warming_up",
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
            center_source="fresh_rolling" if ready else "warming_up",
        )


def build_strategy(conf: StrategyConf):
    if conf.name == "stable_basis":
        if conf.center_bps is None:
            raise ValueError("stable_basis requires center_bps")
        return StableBasisStrategy(
            center_bps=conf.center_bps,
            upper_bps=conf.upper_bps,
            lower_bps=conf.lower_bps,
            center_mode=conf.center_mode,
            center_window_hours=conf.center_window_hours,
            center_update_minutes=conf.center_update_minutes,
            center_min_coverage_ratio=conf.center_min_coverage_ratio,
            center_min_samples=conf.center_min_samples,
            center_last_valid_max_age_hours=conf.center_last_valid_max_age_hours,
            center_max_latest_sample_age_sec=conf.center_max_latest_sample_age_sec,
        )
    if conf.name == "drifting_basis":
        if conf.window_minutes is None:
            raise ValueError("drifting_basis requires window_minutes")
        return DriftingBasisStrategy(
            window_minutes=conf.window_minutes,
            upper_bps=conf.upper_bps,
            lower_bps=conf.lower_bps,
        )
    raise ValueError(f"unknown strategy {conf.name!r}")
