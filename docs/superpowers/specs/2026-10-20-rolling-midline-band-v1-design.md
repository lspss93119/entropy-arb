# Rolling Midline and Band v1 Design

## Scope

Add a pure, dependency-free rolling parameter calculator and an isolated
historical replay comparison. This phase does not import the Engine, change
order planning, change Execution Journal behavior, alter configuration, or
send orders.

The implementation is based on the verified `origin/main` merge commit
`8857f31b8e275ef8911291acaac39d2c3c15dfa1` and the branch
`feature/rolling-midline-band-v1`.

## Two explicitly separate methods

The existing research method remains authoritative for comparison:

1. The Recorder Premium is the mid-to-mid value
   `(entropy_mid / hedge_mid - 1) * 10_000`.
2. The legacy analyzer computes `M = round(median(premium_close_bps), 1)`.
3. It computes executable room from recorded executable edges, not from
   Premium deviations:

   - `sell_room = sell_edge_max_bps - M - fees_bps`
   - `buy_room = buy_edge_max_bps + M - fees_bps`
   - `upper = max(round(P90(sell_room) * 2) / 2, 1.0)`
   - `lower = max(round(P90(buy_room) * 2) / 2, 1.0)`

The new calculator implements the requested Premium-deviation method only as
a comparison method, never as a production strategy setting:

1. Use `premium_close_bps` from the Recorder; do not substitute a different
   Premium definition or aggregation.
2. `M = median(premium_close_bps)` without implicit fee or edge conversion.
3. `D+ = {premium - M | premium > M}` and
   `D- = {M - premium | premium < M}`.
4. `upper = P90(D+)` and `lower = P90(D-)`.

Both P90 calculations use the existing linear interpolation convention:
`rank = (n - 1) * 0.90`; interpolate between floor and ceiling ranks.
Equality-to-midline samples are not assigned to either tail. The replay
report keeps both method names and formulas visible and never ranks a method
solely by trigger count.

## Rolling window and causality

- Window length: 8 hours.
- Update interval: 60 minutes.
- Update timestamps are aligned to UTC hour boundaries.
- At `effective_at`, the window is `[effective_at - 8h, effective_at)`;
  samples at `effective_at` are excluded.
- `window_end` is the exclusive end of that interval and equals
  `effective_at`.
- A parameter row is generated only from data already completed before the
  update timestamp. No future row, same-boundary row, forward fill, or
  cross-segment join is allowed.

## Quality gate

The default calculator accepts only a complete, single-market minute window:

- exactly 480 unique minute timestamps;
- every expected 60-second slot is present;
- no duplicate timestamp;
- every Premium is finite;
- when Recorder `samples` is supplied, every minute has at least 10 valid
  one-second samples;
- all input rows have the same explicit market key;
- both tails have at least 10 observations and both resulting bands are
  positive and finite.

`coverage` is the number of accepted unique minute samples divided by 480.
Any failed condition returns `valid=False`, preserves diagnostic metadata,
and sets a stable `invalid_reason`; it does not return a usable parameter.

## Module contract

The production-side module is pure Python and accepts typed timestamped
samples. It returns immutable parameter records containing:

`effective_at`, `window_start`, `window_end`, `sample_count`, `coverage`,
`midline_bps`, `upper_bps`, `lower_bps`, `valid`, and `invalid_reason`.

The module exposes a single-window calculation and a UTC-aligned iterator for
the 60-minute update schedule. Market identity is an input invariant, not an
inferred join key.

## Historical validation

The validation uses the seven canonical long-history exports in
`research/data/long-history-export/`. It treats each recorded contiguous
segment independently and reports the seven market/venue pairs separately.
The Phase 1 snapshot is not silently appended again because the canonical
long-history exports already retain source provenance and overlap decisions.

For every valid rolling update, the replay records both parameter methods and
compares them with the existing fixed-band replay. It reports, by market and
method:

- valid/invalid windows and invalid reasons;
- SELL/BUY signal counts and completed round trips;
- maximum inventory and ending inventory;
- ending mark-to-market, clearly labeled as unrealized valuation rather than
  realized PnL;
- cost sensitivity at 0, 0.5, 1, and 2 bps per leg;
- segment/date stability and whether any window crossed a gap.

The replay is an observed-minute BBO model, not a fill-level backtest. It
does not claim queue position, latency, partial-fill behavior, or realized
trading PnL.

## Non-goals

- No Engine or config integration.
- No change to Fixed Midline / Band or Inventory Ladder behavior.
- No change to Execution Journal v1.
- No new strategy threshold or production setting.
- No deployment, live process, credential access, or order submission.
