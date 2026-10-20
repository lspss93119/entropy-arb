"""Pure rolling midline and band calculations.

This module deliberately has no venue, configuration, Engine, or credential
dependencies.  It consumes completed minute samples and returns either a
usable parameter snapshot or an explicit invalid reason.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional, Sequence


WINDOW_SECONDS = 8 * 60 * 60
SAMPLE_INTERVAL_SECONDS = 60
UPDATE_INTERVAL_SECONDS = 60 * 60
DEFAULT_MIN_RECORDER_SAMPLES = 10
DEFAULT_MIN_TAIL_SAMPLES = 10


@dataclass(frozen=True)
class RollingSample:
    """One completed minute Premium observation.

    ``samples`` is the Recorder's count of fresh one-second BBO samples.  It
    is optional so the calculator remains useful for already-normalized
    Premium series; when present it participates in the quality gate.
    """

    timestamp: int
    premium_bps: float
    market: str
    samples: Optional[int] = None


@dataclass(frozen=True)
class RollingParameters:
    """A single rolling parameter snapshot or an invalid diagnostic."""

    effective_at: int
    window_start: int
    window_end: int
    sample_count: int
    coverage: float
    midline_bps: Optional[float]
    upper_bps: Optional[float]
    lower_bps: Optional[float]
    valid: bool
    invalid_reason: Optional[str]


def _quantile(values: Sequence[float], q: float) -> Optional[float]:
    """Return the existing analyzer's linear-interpolated percentile."""

    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = (len(ordered) - 1) * q
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def _invalid(
    effective_at: int,
    window_start: int,
    sample_count: int,
    coverage: float,
    reason: str,
) -> RollingParameters:
    return RollingParameters(
        effective_at=effective_at,
        window_start=window_start,
        window_end=effective_at,
        sample_count=sample_count,
        coverage=coverage,
        midline_bps=None,
        upper_bps=None,
        lower_bps=None,
        valid=False,
        invalid_reason=reason,
    )


def _validate_options(
    window_seconds: int,
    sample_interval_seconds: int,
    update_interval_seconds: int,
    min_recorder_samples: int,
    min_tail_samples: int,
) -> int:
    if window_seconds <= 0 or sample_interval_seconds <= 0:
        raise ValueError("window and sample intervals must be positive")
    if window_seconds % sample_interval_seconds:
        raise ValueError("window must be divisible by sample interval")
    if update_interval_seconds <= 0:
        raise ValueError("update interval must be positive")
    if min_recorder_samples < 0 or min_tail_samples < 1:
        raise ValueError("quality thresholds are out of range")
    return window_seconds // sample_interval_seconds


def calculate_rolling_parameters(
    samples: Iterable[RollingSample],
    effective_at: int,
    *,
    market_key: Optional[str] = None,
    window_seconds: int = WINDOW_SECONDS,
    sample_interval_seconds: int = SAMPLE_INTERVAL_SECONDS,
    update_interval_seconds: int = UPDATE_INTERVAL_SECONDS,
    min_recorder_samples: int = DEFAULT_MIN_RECORDER_SAMPLES,
    min_tail_samples: int = DEFAULT_MIN_TAIL_SAMPLES,
) -> RollingParameters:
    """Calculate one causal Premium-deviation rolling snapshot.

    The window is ``[effective_at - window_seconds, effective_at)``.  The
    exclusive right boundary is intentional: a row stamped at the update
    boundary is not considered completed input for that update.
    """

    expected_count = _validate_options(
        window_seconds,
        sample_interval_seconds,
        update_interval_seconds,
        min_recorder_samples,
        min_tail_samples,
    )
    window_start = effective_at - window_seconds
    records = list(samples)
    selected = [
        sample
        for sample in records
        if isinstance(sample.timestamp, int)
        and window_start <= sample.timestamp < effective_at
    ]
    timestamps = [sample.timestamp for sample in selected]
    unique_count = len(set(timestamps))
    coverage = unique_count / expected_count
    sample_count = len(selected)

    if effective_at % update_interval_seconds != 0:
        return _invalid(
            effective_at,
            window_start,
            sample_count,
            coverage,
            "effective_at_not_aligned",
        )

    if market_key is not None and any(sample.market != market_key for sample in selected):
        return _invalid(
            effective_at, window_start, sample_count, coverage, "market_mismatch"
        )
    if len({sample.market for sample in selected}) > 1:
        return _invalid(
            effective_at, window_start, sample_count, coverage, "mixed_markets"
        )

    if any(timestamp % sample_interval_seconds for timestamp in timestamps):
        return _invalid(
            effective_at,
            window_start,
            sample_count,
            coverage,
            "timestamp_not_aligned",
        )
    if any(count > 1 for count in Counter(timestamps).values()):
        return _invalid(
            effective_at,
            window_start,
            sample_count,
            coverage,
            "duplicate_timestamp",
        )
    if any(
        not isinstance(sample.premium_bps, (int, float))
        or not math.isfinite(float(sample.premium_bps))
        or (
            sample.samples is not None
            and (
                not isinstance(sample.samples, (int, float))
                or not math.isfinite(float(sample.samples))
            )
        )
        for sample in selected
    ):
        return _invalid(effective_at, window_start, sample_count, coverage, "invalid_sample")

    expected_timestamps = set(
        range(window_start, effective_at, sample_interval_seconds)
    )
    if set(timestamps) != expected_timestamps:
        ordered = sorted(set(timestamps))
        has_internal_gap = any(
            right - left > sample_interval_seconds
            for left, right in zip(ordered, ordered[1:])
        )
        reason = "data_gap" if has_internal_gap else "insufficient_window_samples"
        return _invalid(effective_at, window_start, sample_count, coverage, reason)

    if any(
        sample.samples is not None
        and float(sample.samples) < min_recorder_samples
        for sample in selected
    ):
        return _invalid(
            effective_at,
            window_start,
            sample_count,
            coverage,
            "low_recorder_samples",
        )

    premiums = [float(sample.premium_bps) for sample in selected]
    midline = _quantile(premiums, 0.5)
    assert midline is not None
    upper_deviations = [value - midline for value in premiums if value > midline]
    lower_deviations = [midline - value for value in premiums if value < midline]
    if len(upper_deviations) < min_tail_samples:
        return _invalid(
            effective_at,
            window_start,
            sample_count,
            coverage,
            "insufficient_upper_tail_samples",
        )
    if len(lower_deviations) < min_tail_samples:
        return _invalid(
            effective_at,
            window_start,
            sample_count,
            coverage,
            "insufficient_lower_tail_samples",
        )

    upper = _quantile(upper_deviations, 0.90)
    lower = _quantile(lower_deviations, 0.90)
    if (
        upper is None
        or lower is None
        or not math.isfinite(upper)
        or not math.isfinite(lower)
        or upper <= 0.0
        or lower <= 0.0
    ):
        return _invalid(
            effective_at, window_start, sample_count, coverage, "nonpositive_band"
        )

    return RollingParameters(
        effective_at=effective_at,
        window_start=window_start,
        window_end=effective_at,
        sample_count=sample_count,
        coverage=coverage,
        midline_bps=midline,
        upper_bps=upper,
        lower_bps=lower,
        valid=True,
        invalid_reason=None,
    )


def iter_rolling_parameters(
    samples: Iterable[RollingSample],
    *,
    start_at: Optional[int] = None,
    end_at: Optional[int] = None,
    window_seconds: int = WINDOW_SECONDS,
    sample_interval_seconds: int = SAMPLE_INTERVAL_SECONDS,
    update_interval_seconds: int = UPDATE_INTERVAL_SECONDS,
    min_recorder_samples: int = DEFAULT_MIN_RECORDER_SAMPLES,
    min_tail_samples: int = DEFAULT_MIN_TAIL_SAMPLES,
) -> Iterator[RollingParameters]:
    """Yield UTC-aligned snapshots without looking beyond each boundary."""

    records = list(samples)
    if not records:
        return
    _validate_options(
        window_seconds,
        sample_interval_seconds,
        update_interval_seconds,
        min_recorder_samples,
        min_tail_samples,
    )
    timestamps = [sample.timestamp for sample in records if isinstance(sample.timestamp, int)]
    if not timestamps:
        return
    if start_at is None:
        first = min(timestamps) + window_seconds
        start_at = ((first + update_interval_seconds - 1) // update_interval_seconds) * update_interval_seconds
    if end_at is None:
        last = max(timestamps)
        end_at = ((last // update_interval_seconds) + 1) * update_interval_seconds
    if start_at % update_interval_seconds or end_at % update_interval_seconds:
        raise ValueError("rolling iterator bounds must align to update interval")
    current = start_at
    while current < end_at:
        yield calculate_rolling_parameters(
            records,
            current,
            window_seconds=window_seconds,
            sample_interval_seconds=sample_interval_seconds,
            update_interval_seconds=update_interval_seconds,
            min_recorder_samples=min_recorder_samples,
            min_tail_samples=min_tail_samples,
        )
        current += update_interval_seconds
