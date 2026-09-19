# Rolling Window Strategy Design

## Goal

Add a selectable, live-capable rolling-window mean-reversion strategy while
preserving the existing fixed-threshold strategy as the default and keeping
the existing two-leg execution, hedging, reconciliation, and fail-closed
behavior.

## Scope and non-goals

- `strategy.mode: fixed` keeps the current `thresholds.*` signal and order
  behavior unchanged.
- `strategy.mode: rolling` uses completed recorder minute rows to calculate a
  walk-forward rolling mean and population standard deviation.
- The first rolling implementation supports one open spread position at a
  time; it does not pyramid entries.
- Rolling does not change the recorder CSV schema or add a second persistent
  market-data store.
- Rolling never falls back to fixed thresholds when its snapshot is invalid.
- No automatic live-mode activation is added; selecting rolling remains an
  explicit configuration change.

## Configuration

The existing `thresholds` section remains required so a single configuration
file can switch back to fixed mode without being rewritten. New settings are:

```yaml
strategy:
  mode: fixed                 # fixed | rolling

rolling:
  window_hours: 12
  update_minutes: 15
  entry_z: 1.5
  exit_z: 0.5
  min_reversion_bps: 5.0
  max_spread_bps: 10.0
  min_coverage_pct: 80.0
  timeout_hours: 12.0
  seed_from_csv: true
```

The loader validates the mode, positive window/update/timeout values,
`0 <= exit_z < entry_z`, non-negative bps gates, and coverage in `(0, 100]`.

## Rolling data flow

1. `MinuteRecorder` writes a completed minute row exactly as it does today.
2. After the row is flushed, it invokes an optional synchronous callback with
   the row mapping. The callback is only a consumer hook; it does not change
   CSV output.
3. The rolling calculator keeps one deduplicated in-memory point per
   `minute_ts`, using `premium_close_bps` only when `samples > 0` and the value
   is finite.
4. When evaluating a book update, the calculator uses the current
   `update_minutes` block. The snapshot window is
   `[block_start - window_hours, block_start)`, so rows from the current block
   cannot affect its signal.
5. A snapshot is valid only when its valid-row count reaches the configured
   coverage minimum and its standard deviation is finite and positive.

## Signals

For a current mid-to-mid premium `p` and valid snapshot `(mean, std)`:

```text
z = (p - mean) / std
```

Entry requires all of:

- `abs(z) >= entry_z`;
- `abs(p - mean) >= min_reversion_bps`;
- both current top-of-book spreads are `<= max_spread_bps`;
- the existing `plan_arb` can find a current executable two-leg slice after
  venue fees and inventory/cap checks;
- the existing persistence and cooldown gates pass.

Positive z enters `sell_entropy` and negative z enters `buy_entropy`. The
rolling statistical gate is not treated as executable profit: `plan_arb` still
must validate the live BBO and fees.

While a rolling spread position is open, no new entry is allowed. Exit is the
opposite two-leg direction when:

- `abs(z) <= exit_z` and both spreads are within the spread gate; or
- the position reaches `timeout_hours`, in which case the exit ignores the
  spread gate but still requires fresh books.

Exit plans are reduce-only on both legs and may cross a temporarily negative
edge because closing risk is more important than preserving the entry hurdle.
Residuals continue through the existing hedge and reconciliation paths.

At rolling startup, non-flat reconciled positions cause a clear startup error;
the engine does not guess the direction of an inherited spread position.

## Execution and logging

- Fixed mode continues to use `_eff_threshold()` and the current `plan_arb`
  edge checks.
- Rolling entry uses threshold `0` for the executable plan; fees are still
  applied by `plan_arb`.
- Rolling exit uses a force-close plan and `reduce_only=True`.
- Existing per-venue locks, concurrent leg sends, settlement, residual hedge,
  venue outage pause, and exchange-authoritative reconciliation remain in
  force.
- `runs-*.csv` records strategy mode and rolling parameters for live runs.
- `trades-*.csv` records mode, rolling z/mean/std/coverage/block, exit reason,
  and whether the primary legs were reduce-only.
- Missing/invalid rolling snapshots fail closed for new entries; there is no
  silent fixed-strategy fallback.

## Verification

Tests will cover:

- config defaults and rolling validation;
- strict walk-forward windows, coverage, zero dispersion, entry/exit/timeout
  gates, and CSV seeding;
- force-close plan construction;
- recorder callback timing;
- fixed engine behavior unchanged;
- rolling entry, no-pyramiding, reduce-only exit, startup flat guard, and
  run/trade logging fields;
- full `python3 -m pytest -q`, compile checks, and an offline rolling replay
  using a small generated recorder CSV.
