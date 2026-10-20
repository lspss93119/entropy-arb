"""Contract tests for the isolated rolling midline/band calculator."""

from __future__ import annotations

import math

import pytest

from entropy_arb.rolling import (
    RollingSample,
    calculate_rolling_parameters,
    iter_rolling_parameters,
)


INTERVAL = 60
WINDOW = 8 * 60 * 60
EFFECTIVE_AT = 1_700_031_600  # UTC hour boundary


def make_samples(
    *,
    effective_at: int = EFFECTIVE_AT,
    values: list[float] | None = None,
    market: str = "io:SNDK x lighter:SNDK",
    samples_per_minute: int | None = 10,
) -> list[RollingSample]:
    if values is None:
        values = [0.0] * (WINDOW // INTERVAL)
    assert len(values) > 0
    start = effective_at - len(values) * INTERVAL
    return [
        RollingSample(
            timestamp=start + index * INTERVAL,
            premium_bps=value,
            market=market,
            samples=samples_per_minute,
        )
        for index, value in enumerate(values)
    ]


def test_known_premium_deviation_median_and_linear_p90() -> None:
    values = [-10.0] * 10 + [-2.0] * 10 + [0.0] * 440 + [2.0] * 10 + [10.0] * 10
    result = calculate_rolling_parameters(make_samples(values=values), EFFECTIVE_AT)

    assert result.valid is True
    assert result.sample_count == 480
    assert result.coverage == 1.0
    assert result.midline_bps == 0.0
    assert result.upper_bps == 10.0
    assert result.lower_bps == 10.0
    assert result.window_start == EFFECTIVE_AT - WINDOW
    assert result.window_end == EFFECTIVE_AT
    assert result.invalid_reason is None


def test_linear_interpolation_is_used_for_tail_p90() -> None:
    values = [0.0] * 478 + [1.0, 3.0]
    result = calculate_rolling_parameters(make_samples(values=values), EFFECTIVE_AT)

    assert result.valid is False
    assert result.invalid_reason == "insufficient_upper_tail_samples"

    # A direct unit check through a balanced tail makes the interpolation
    # observable without weakening the strict two-tail quality gate.
    values = [-1.0] * 10 + [-3.0] * 2 + [0.0] * 456 + [1.0] * 10 + [3.0] * 2
    result = calculate_rolling_parameters(make_samples(values=values), EFFECTIVE_AT)
    assert result.valid is True
    assert result.upper_bps == pytest.approx(2.8)
    assert result.lower_bps == pytest.approx(2.8)


def test_sample_at_effective_boundary_is_excluded() -> None:
    values = [-1.0] * 10 + [0.0] * 460 + [1.0] * 10
    samples = make_samples(values=values)
    samples.append(
        RollingSample(
            timestamp=EFFECTIVE_AT,
            premium_bps=999.0,
            market=samples[0].market,
            samples=10,
        )
    )
    result = calculate_rolling_parameters(samples, EFFECTIVE_AT)

    assert result.valid is True
    assert result.sample_count == 480
    assert result.midline_bps == 0.0


def test_startup_shortage_is_invalid() -> None:
    result = calculate_rolling_parameters(make_samples(values=[0.0] * 479), EFFECTIVE_AT)

    assert result.valid is False
    assert result.invalid_reason == "insufficient_window_samples"
    assert result.coverage == pytest.approx(479 / 480)


def test_missing_minute_is_invalid_and_not_forward_filled() -> None:
    samples = make_samples()
    del samples[120]
    result = calculate_rolling_parameters(samples, EFFECTIVE_AT)

    assert result.valid is False
    assert result.invalid_reason == "data_gap"
    assert result.midline_bps is None
    assert result.upper_bps is None
    assert result.lower_bps is None


def test_duplicate_timestamp_is_invalid() -> None:
    samples = make_samples()
    samples.append(samples[100])
    result = calculate_rolling_parameters(samples, EFFECTIVE_AT)

    assert result.valid is False
    assert result.invalid_reason == "duplicate_timestamp"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_premium_is_invalid(value: float) -> None:
    samples = make_samples()
    samples[10] = RollingSample(
        timestamp=samples[10].timestamp,
        premium_bps=value,
        market=samples[10].market,
        samples=10,
    )
    result = calculate_rolling_parameters(samples, EFFECTIVE_AT)

    assert result.valid is False
    assert result.invalid_reason == "invalid_sample"


def test_mixed_markets_are_rejected() -> None:
    samples = make_samples()
    samples[200] = RollingSample(
        timestamp=samples[200].timestamp,
        premium_bps=samples[200].premium_bps,
        market="io:SNDK x lighter-rh:SNDK",
        samples=10,
    )
    result = calculate_rolling_parameters(samples, EFFECTIVE_AT)

    assert result.valid is False
    assert result.invalid_reason == "mixed_markets"


def test_low_recorder_sample_count_is_invalid() -> None:
    samples = make_samples()
    samples[200] = RollingSample(
        timestamp=samples[200].timestamp,
        premium_bps=samples[200].premium_bps,
        market=samples[200].market,
        samples=9,
    )
    result = calculate_rolling_parameters(samples, EFFECTIVE_AT)

    assert result.valid is False
    assert result.invalid_reason == "low_recorder_samples"


def test_update_boundary_must_be_utc_hour_aligned() -> None:
    result = calculate_rolling_parameters(make_samples(), EFFECTIVE_AT + 60)

    assert result.valid is False
    assert result.invalid_reason == "effective_at_not_aligned"


def test_tail_quality_is_required() -> None:
    result = calculate_rolling_parameters(make_samples(values=[0.0] * 480), EFFECTIVE_AT)

    assert result.valid is False
    assert result.invalid_reason == "insufficient_upper_tail_samples"


def test_iterator_uses_only_completed_rows_and_hour_updates() -> None:
    values = [-1.0 if index % 2 == 0 else 1.0 for index in range(660)]
    samples = make_samples(effective_at=EFFECTIVE_AT + 3 * 3600, values=values)
    samples.append(
        RollingSample(
            timestamp=EFFECTIVE_AT + 3 * 3600,
            premium_bps=1000.0,
            market=samples[0].market,
            samples=10,
        )
    )
    results = list(
        iter_rolling_parameters(
            samples,
            start_at=EFFECTIVE_AT,
            end_at=EFFECTIVE_AT + 4 * 3600,
        )
    )

    assert [item.effective_at for item in results] == [
        EFFECTIVE_AT,
        EFFECTIVE_AT + 3600,
        EFFECTIVE_AT + 2 * 3600,
        EFFECTIVE_AT + 3 * 3600,
    ]
    assert results[0].valid is True
    assert results[-1].valid is True
    assert results[-1].midline_bps != 1000.0


def test_explicit_market_key_is_enforced() -> None:
    result = calculate_rolling_parameters(
        make_samples(), EFFECTIVE_AT, market_key="other-market"
    )

    assert result.valid is False
    assert result.invalid_reason == "market_mismatch"


def test_result_never_contains_nonfinite_valid_parameters() -> None:
    values = [-1.0] * 10 + [0.0] * 460 + [1.0] * 10
    result = calculate_rolling_parameters(make_samples(values=values), EFFECTIVE_AT)

    assert result.valid is True
    assert all(
        math.isfinite(value)
        for value in (result.midline_bps, result.upper_bps, result.lower_bps)
    )
