# Delta-Neutral Builder Strategy Design

## Status and scope

This document is a design specification only. It does not implement the
strategy, change the configuration parser, change the Engine, or change live
trading behavior.

The proposed feature adds an explicitly selected
`delta_neutral_builder` strategy to entropy-arb. It slowly adds matched
two-leg exposure toward a session-local target when the executable cross-venue
basis is favorable. It is a position-building strategy, not a repeatedly
opening-and-closing arbitrage strategy.

The active trading configuration must select exactly one order-producing
strategy. The builder cannot run concurrently with `stable_basis` (or with
any other strategy). Existing `stable_basis` behavior remains the compatibility
baseline. The repository currently also contains `drifting_basis`; this design
does not alter its semantics, and it likewise cannot run concurrently with the
builder.

## 1. Purpose and non-goals

### Purpose

For every new process startup, the builder should:

1. Capture the actual Entropy and hedge venue positions as a session baseline.
2. Leave any pre-existing imbalance untouched.
3. Add matched exposure in fixed-size chunks in one configured direction.
4. Enter only when the executable BBO basis is favorable relative to the
   effective strategy center.
5. Stop at the session's additional matched-notional target and hold.

The builder must prefer no trade over an unsafe, stale, oversized, or
unfavorable entry.

### Non-goals

This feature does not add or change:

- Funding Monitor, funding-based decisions, or net-carry calculations.
- Market diagnosis, regime detection, or automatic strategy selection/switching.
- Maker-first execution, TWAP urgency, or a time-based build horizon.
- Excellent/Good/Neutral basis tiers or any other multi-tier quality model.
- Automatic correction of an existing imbalance.
- Automatic unwind or flattening after completion.
- A second execution engine, matching engine, hedge venue, or order protocol.
- Rolling-center semantics, premium definition, websocket/quota behavior, or
  execution slippage.
- Changes to stable-basis thresholds, inventory surcharge, sizing semantics,
  reconciliation, risk limits, or fee calculations.

## 2. Existing architecture and proposed boundary

### Current production flow

The current repository has these relevant boundaries:

- `Config.strategy` is parsed into a typed `StrategyConf`; `build_strategy()`
  creates the one selected strategy.
- `StableBasisStrategy` and `DriftingBasisStrategy` expose center/readiness
  through `StrategyState`. They do not know venues, credentials, orders, or
  positions.
- `Engine._run_inner()` creates the two venue adapters, loads market metadata,
  bootstraps the optional rolling center, starts feed/recorder/reference tasks,
  and owns shutdown.
- `Engine._scan()` reads one immutable `StrategyState` snapshot and currently
  applies freshness, venue readiness, locks, rate limits, persistence,
  inventory surcharge, position headroom, and `plan_arb()` before execution.
- `Engine._execute_locked()` allocates an `execution_id` and invokes the
  existing simultaneous two-leg taker settlement path.
- `Engine._execute()` owns actual leg fills, fees, fill-edge accounting,
  lifecycle telemetry, and the transition into `_maybe_hedge()`.
- `_maybe_hedge()`, `_hedge()`, and reconciliation own residual delta handling;
  `execution_id` is already the correlation key for lifecycle updates.

The existing `plan_arb()` is specifically a fee-aware arbitrage depth planner:
it requires a fee-clearing executable edge and sizes from crossable depth. A
builder entry is deliberately allowed to be a non-arbitrage, fixed-size
position-building trade, so the builder must not force its condition through
the old arbitrage threshold semantics.

### Smallest clean boundary

The implementation should add a focused builder domain component, preferably
in a new `entropy_arb/builder.py`, rather than growing a large conditional
block inside `engine.py`.

Conceptually, the boundary is:

```text
fresh Entropy/hedge BBO + StrategyState
                 |
                 v
        BuilderMarketSnapshot
                 |
                 v
       DeltaNeutralBuilderStrategy
                 |
                 v
        BuilderDecision / no-entry
                 |
                 v
 Engine common safety/capacity gates
                 |
                 v
  venue-neutral two-leg execution intent
                 |
                 v
 existing simultaneous execution / hedge / reconcile
                 |
                 v
 execution lifecycle settlement callback
                 |
                 v
       BuilderSession progress
```

The builder domain component may contain only pure calculations and explicit
session state. Its inputs should be plain values: direction, effective center
and source, executable BBO, remaining target, capacity, and lifecycle results.
It must not import venue adapters, credential/signing modules, order APIs, or
reconciliation code.

The Engine remains the owner of:

- main-feed freshness and fail-closed STALE behavior;
- venue readiness, outage state, locks, rate limits, and minimums;
- position-cap/headroom and existing risk checks;
- mapping an intent to the two concrete venue objects;
- leg slippage, fees, simultaneous sends, partial fills, emergency hedge,
  reconciliation, and lifecycle telemetry;
- invoking the builder only once per eligible evaluation and notifying it once
  the existing lifecycle has a final matched-exposure result.

The preferred implementation shape is a narrow venue-neutral execution intent
or plan DTO consumed by the existing settlement routine. This avoids reusing
`plan_arb()`'s arbitrage-only acceptance rule while avoiding a new execution
engine. The legacy `ArbPlan` path and all stable-basis planning remain
unchanged.

## 3. Configuration schema

The strategy is selected explicitly, with no signed per-venue targets:

```yaml
strategy:
  name: delta_neutral_builder
  params:
    direction: long_entropy

    target_additional_notional_usd: 5000
    chunk_usd: 100
    entry_offset_bps: 1.0

    success_cooldown_sec: 60
    failed_retry_sec: 5

    center_mode: rolling
    center_bps: -1.8
    center_window_hours: 12
    center_update_minutes: 60
    # Existing rolling-center availability parameters remain reusable:
    # center_min_coverage_ratio: 0.70
    # center_min_samples: 60
    # center_last_valid_max_age_hours: 6
    # center_max_latest_sample_age_sec: 300
```

Validation requirements:

- `name` must be exactly `delta_neutral_builder` for this strategy.
- `direction` must be exactly `long_entropy` or `short_entropy`.
- `target_additional_notional_usd`, `chunk_usd`, and `entry_offset_bps` must
  be finite and positive.
- `success_cooldown_sec` and `failed_retry_sec` must be finite and
  non-negative; their defaults are 60 and 5 seconds.
- `center_bps` remains the required fixed fallback center. `center_mode` is
  `fixed` or `rolling`; when rolling is selected, all existing rolling-center
  availability parameters use the existing parser and strategy implementation.
- Builder-only parameters are rejected for `stable_basis`; stable-only
  parameters are not silently translated into builder behavior.
- Unknown strategy names and unknown parameters remain startup errors.

The existing global `execution.cooldown_sec` remains an outer Engine safety
floor. The builder's effective wait is at least its configured builder delay
and at least the existing global cooldown, so enabling the builder cannot
shorten an existing configured safety interval.

## 4. Session baseline semantics

Every process startup creates a new builder session. A session identifier is
local to the process and is not restored from disk.

After the existing strict startup position reconciliation succeeds, the Engine
captures:

- signed Entropy base quantity and current signed USD position value;
- signed hedge/Lighter base quantity and current signed USD position value;
- configured direction;
- target and zero initial built progress.

The USD display value is calculated from each venue's own current reference
price (normally its loaded BBO midpoint). Signed quantities remain the
authoritative baseline if a display price is temporarily unavailable.

The baseline is observational only. It is never compared across venues to
generate a correction order. For example:

```text
baseline:
  Entropy +$1,000
  Lighter  -$700
direction: long_entropy
target additional: $5,000 / side
```

The builder adds approximately `Entropy +$5,000` and `Lighter -$5,000`; the
pre-existing $300 imbalance remains. Current positions and baseline positions
are displayed separately.

If strict startup position reads fail, the builder does not start issuing
entries. Feed/status and existing shutdown/error handling remain available, but
there is no safe baseline from which to build.

## 5. Progress accounting

`built_matched_notional_usd` is session-local, monotonic, and based only on a
finalized execution lifecycle. It is not inferred from requested size,
submitted size, current position delta, expected edge, or fill edge.

For a finalized execution:

```text
matched_qty = min(actual Entropy-side filled qty,
                  actual hedge-side filled qty)

matched_notional_usd = matched_qty
                        * min(actual Entropy avg fill price,
                              actual hedge avg fill price)
```

The minimum of the two matched leg notionals is a conservative common
per-side exposure measure. It is gross exposure, not P&L, reward, or fill-edge
accounting. Actual average fill prices are required; a missing/unknown fill
price leaves the lifecycle unresolved for builder progress rather than being
replaced with an intended limit price.

Progress rules:

- Full two-leg fills add the actual common matched notional.
- Partial two-leg fills add only the actual matched portion after the residual
  hedge lifecycle settles.
- A one-leg fill followed by a hedge-away has `matched_qty == 0` and adds zero.
- A failed, rejected, or unresolved lifecycle adds zero until it becomes
  authoritative; unresolved state must not be converted to zero prematurely.
- Each `execution_id` is applied at most once. Repeated lifecycle events are
  updates, not additional progress.

The requested chunk is never counted as progress. For example, a requested
`$100 / side` execution that settles with `$40 / side` of common matched
exposure adds `$40`, not `$100`.

The builder may retain a small in-memory map from existing `execution_id` to
pending result. It must not restore builder progress or session state from
historical CSV/database rows on restart.

## 6. Direction and executable-basis formulas

The center remains the existing Entropy-vs-hedge midpoint premium:

```text
premium_bps = (Entropy_mid / Hedge_mid - 1) * 10,000
```

The builder entry signal does **not** use that midpoint premium. It uses the
prices that the two taker legs can actually hit at the top of book. The
builder's executable basis is always expressed in Entropy-vs-hedge ratio
orientation, so its sign is explicit:

### `long_entropy`

Execution legs:

```text
BUY  Entropy at Entropy ask
SELL hedge at hedge bid
```

```text
executable_basis_bps = (Entropy_ask / Hedge_bid - 1) * 10,000
entry_trigger_bps = effective_center_bps - entry_offset_bps
```

Enter only when:

```text
executable_basis_bps <= entry_trigger_bps
```

### `short_entropy`

Execution legs:

```text
SELL Entropy at Entropy bid
BUY  hedge at hedge ask
```

```text
executable_basis_bps = (Entropy_bid / Hedge_ask - 1) * 10,000
entry_trigger_bps = effective_center_bps + entry_offset_bps
```

Enter only when:

```text
executable_basis_bps >= entry_trigger_bps
```

The comparisons are inclusive at the configured threshold. There is one
threshold per direction and no additional quality tiers. The existing
`calculate_premiums()` values may be used as raw inputs, but the implementation
must not accidentally use `buy_edge_bps` with the opposite sign for
`long_entropy`.

The trigger uses raw executable BBO basis as specified. Existing fee
calculation and leg slippage are still applied by the production execution
stack; the builder must not invent a second fee or slippage formula, and it
must not relabel a builder entry as arbitrage profit.

## 7. Signal and candidate evaluation

Before the builder can return an entry intent, the Engine must establish the
same common safety prerequisites used by existing trading:

- both books are ready and have a valid two-sided BBO;
- both feeds are fresh under the configured staleness limit;
- the main Entropy feed is not STALE, in outage, rate-limited, or locked;
- the hedge feed is likewise ready, fresh, available, and unlocked;
- no unresolved lifecycle or Engine halt blocks execution;
- existing rate limits, minimum order notional, position caps, and risk gates
  leave positive capacity.

The builder then receives the immutable effective center snapshot. These center
sources are all valid for builder entry:

```text
fresh_rolling
last_valid
fixed_fallback
```

The builder does not require `fresh_rolling`; it does require a usable center
and the existing main-book freshness gate.

The builder does not apply `inventory_scale_bps` or call the stable-basis
inventory surcharge. Its safety comes from target remaining, capacity, caps,
and the common Engine risk gates.

## 8. Order size and target clamping

Each valid signal requests at most one fixed-size chunk per evaluation:

```text
requested_chunk_usd = chunk_usd
```

The Engine clamps this request, without intentional overshoot, by:

1. remaining session target;
2. existing `min_order_notional` and `max_order_notional`;
3. current per-venue position-cap headroom in the configured direction;
4. existing depth, size-step, rate-limit, and risk constraints.

The resulting two legs use matched quantity as far as the existing planner and
venue precision allow. If the remaining target is positive but below the
effective minimum order size, the builder emits no entry and reports a
non-trading `TARGET_REMAINDER_BELOW_MINIMUM`/hold reason rather than
overshooting.

The builder never layers multiple entries. At most one builder execution may
be in flight for a session, preventing concurrent requests from overshooting
the remaining target before progress is settled.

## 9. Cooldown/retry state machine

The builder's status is derived from explicit session state:

| State | Meaning | Entry allowed |
|---|---|---:|
| `WAIT BASIS` | Books/center are valid but executable basis is not at the one configured trigger, or a common safety gate is closed. | No |
| `READY` | A valid trigger and positive clamped chunk exist; execution is about to be submitted. | One |
| `EXECUTING` | One two-leg execution is in flight or awaiting authoritative lifecycle settlement. | No |
| `RETRY Ns` | The last fully settled attempt added zero matched exposure; retry timer is active. | No |
| `COOLDOWN Ns` | The last fully settled attempt added non-zero matched exposure; success timer is active. | No |
| `COMPLETE / HOLD` | Session matched progress reached the configured target. | No |

Transitions:

- Startup begins at `WAIT BASIS` with zero session progress.
- A valid trigger moves to `READY`, then `EXECUTING` when the existing Engine
  submits the two legs.
- A final lifecycle with non-zero matched exposure increments progress and
  starts `success_cooldown_sec` (default 60s).
- A final lifecycle with zero matched exposure starts `failed_retry_sec`
  (default 5s). It does not start the success cooldown.
- A pending/unresolved hedge remains `EXECUTING`/unresolved and does not start
  either timer or count progress. Existing reconciliation remains responsible
  for resolving it.
- After a timer expires, the next evaluation returns to `WAIT BASIS` or
  `READY` according to current data.
- Reaching the target transitions to `COMPLETE / HOLD` immediately after the
  authoritative settlement update.

The existing global Engine cooldown is an additional floor as described in the
configuration section. Stale feeds, venue outages, rate limits, risk limits,
and Engine halt can keep the builder in a non-entry state regardless of its
builder timer.

## 10. Interaction with the existing execution engine

The builder must reuse the production path rather than introduce a separate
order subsystem:

- Existing simultaneous two-leg taker submission is used.
- Existing `leg_slippage_bps` and venue price-rounding are used unchanged.
- Existing partial-fill, one-leg, unresolved, and emergency-hedge handling is
  used unchanged.
- Existing `execution_id` correlates the builder intent to every hedge and
  final lifecycle update.
- Existing fee calculation, `fill_edge`, `total_fill_edge`, reconcile, rate
  limits, and STALE behavior remain authoritative.

The only builder-specific Engine responsibilities are to translate a
`BuilderDecision` into a venue-neutral two-leg execution intent, start the
session's in-flight guard, and send the finalized matched-notional result back
to the session. The existing settlement code must not be duplicated or
forked.

Builder expected basis/entry quality must not be added to `total_fill_edge` or
treated as realized P&L. A builder execution can have a negative arbitrage
edge by design; progress is exposure, while lifecycle accounting remains the
source of realized execution economics.

## 11. Target completion behavior

When:

```text
built_matched_notional_usd >= target_additional_notional_usd
```

the builder enters `COMPLETE / HOLD`:

- no new builder entry order is generated;
- no target overshoot is attempted;
- existing positions remain in place;
- no automatic unwind or imbalance correction occurs;
- feeds, status/dashboard, recorder, and existing reconciliation continue.

Completion is session-local. A process restart creates a fresh baseline and
sets progress to zero, even if the previous process had completed its target.

## 12. Interaction with the rolling center

The builder consumes the existing `StrategyState` center and does not duplicate
rolling-history, coverage, freshness, persistence, or median code.

The following center sources are explicitly allowed:

- `fresh_rolling`;
- `last_valid`;
- `fixed_fallback`.

The existing causal midpoint-to-midpoint premium definition and 12-hour
rolling availability rules remain unchanged. If the center is updated between
evaluations, the next immutable Engine snapshot is used; an in-flight intent
retains the center/basis diagnostics captured when it was admitted.

No future sample, executable edge, reference-price series, or inventory value
may be introduced into the rolling center by this feature.

## 13. Inventory, risk, and safety invariants

`delta_neutral_builder` intentionally accumulates exposure toward its target,
so it does not use stable-basis `inventory_scale_bps` in the entry threshold.
The existing inventory surcharge implementation and behavior for
`stable_basis` are not removed, changed, or bypassed on its path.

Builder safety is instead provided by:

- session remaining target and no-overshoot clamp;
- current venue position caps and headroom;
- existing min/max order notional and size precision;
- existing staleness, outage, rate-limit, lock, and halt gates;
- existing execution/hedge/reconcile lifecycle;
- no concurrent builder execution.

The following invariants are mandatory:

- STALE main Entropy market data means no builder execution.
- A stale or empty hedge BBO also means no builder execution.
- No fallback trading on reference data is introduced.
- No wallet, signer, credential, or order API is imported by builder domain
  logic.
- No automatic strategy diagnosis, selection, switching, or concurrent order
  generation exists.
- Existing quota-aware websocket behavior is untouched.

## 14. Dashboard and status behavior

Use the existing dashboard/status conventions and existing read-only status
loop. Do not create a second UI.

The builder panel/status should make the session and economics explicit:

```text
Delta-Neutral Builder
-----------------------------------------
Direction       LONG Entropy / SHORT Lighter

Target          $5,000 additional / side
Built           $1,700
Progress        34.0%

Baseline
Entropy         +$1,000
Lighter         -$700

Current positions
Entropy         +$2,700
Lighter         -$2,400

Center          -6.02 bps
Center source   fresh_rolling

Executable      -7.34 bps
Entry trigger   <= -7.02 bps
Entry advantage +0.32 bps

Chunk           $100

Status          WAIT BASIS
```

Status values should include `READY`, `EXECUTING`, `RETRY 3s`, `COOLDOWN 42s`,
and `COMPLETE / HOLD` using the existing project style. Display actual
positions separately from baseline and builder progress. Progress must never
be calculated as current position minus baseline because external fills,
mark-to-market changes, and residual hedges would make that ambiguous.

The dashboard/status path is read-only: it must not update strategy state,
submit orders, reconcile positions, or alter timers.

## 15. Error and restart behavior

- Unknown or malformed builder configuration is a startup error; there is no
  fallback to stable-basis or automatic selection.
- Missing/invalid BBO, stale main feed, stale hedge feed, venue outage, risk
  limit, cap, rate limit, or unresolved lifecycle produces no new entry.
- Existing Engine halt behavior remains authoritative. The builder must not
  bypass a halt or turn an unresolved order into a failed zero-progress result.
- Position-read failure at startup prevents builder entry because the baseline
  cannot be trusted.
- A process restart starts a new session from current actual positions;
  previous session progress, cooldown, pending builder state, and persisted
  center are not used to resume builder progress. Existing rolling-center
  persistence remains independent and reusable for the center itself.
- Reconciliation remains available after completion and after errors, but does
  not become an automatic unwind or baseline-correction mechanism.

## 16. Test matrix

Tests should be pure/fake-data tests and must not require credentials or send
live orders.

### Strategy/config boundary

1. `stable_basis` remains unchanged under equivalent existing configuration.
2. Builder and other order-producing strategies are mutually exclusive.
3. Unknown builder direction/name/parameter fails validation.
4. `long_entropy` maps to BUY Entropy / SELL hedge.
5. `short_entropy` maps to SELL Entropy / BUY hedge.
6. Fixed-fallback center is allowed.
7. Rolling and last-valid centers are allowed.
8. Builder does not apply inventory surcharge.
9. Stable-basis inventory surcharge remains unchanged.

### Executable signal and sizing

10. Long basis uses Entropy ask and hedge bid.
11. Short basis uses Entropy bid and hedge ask.
12. Long enters only at `executable <= center - offset`, including the exact
    boundary.
13. Short enters only at `executable >= center + offset`, including the exact
    boundary.
14. Midpoint basis alone cannot trigger a builder entry.
15. Fixed `$100` chunk is requested by default.
16. Remaining target clamps the chunk.
17. Venue caps, existing minimum/maximum notional, precision, and risk gates
    clamp or reject the chunk.
18. The target cannot be intentionally overshot.
19. STALE/empty books prevent execution.

### Lifecycle/progress/timers

20. Full two-leg success increases progress by actual common matched exposure.
21. Partial execution increases progress only by final matched exposure.
22. One-leg plus hedge-away produces zero builder progress.
23. Failed or unresolved execution does not count intended quantity.
24. Zero finalized progress starts the 5-second retry delay.
25. Successful non-zero progress starts the 60-second cooldown.
26. A pending hedge starts neither timer and remains unresolved.
27. Completion stops additional entries and holds positions.
28. Each execution ID is applied once despite repeated lifecycle updates.

### Session/restart/compatibility

29. Restart captures current positions as a new baseline and resets session
    progress to zero.
30. Existing imbalance is not corrected.
31. Existing execution/hedge/reconcile lifecycle and telemetry semantics remain
    unchanged.
32. Existing websocket/quota mitigation tests remain unchanged and passing.
33. Dashboard/status displays baseline, current positions, center/source,
    executable basis, progress, and builder status without mutating state.

## 17. Explicit stable-basis invariants

When `stable_basis` is selected, the implementation must preserve exactly the
current behavior:

- same fixed or existing rolling center source and center values;
- same `_eff_threshold()` arithmetic, including inventory surcharge;
- same sell-Entropy and buy-Entropy direction classification;
- same `plan_arb()` depth walk, fee handling, quantity, limits, persistence,
  cooldown, and firing behavior;
- same execution, hedge, reconciliation, rate-limit, stale-feed, and quota
  behavior;
- same `fill_edge`, `total_fill_edge`, execution telemetry, and trade CSV
  semantics;
- no builder session, target, progress, or builder timer is created on the
  stable path.

The builder must be an explicit alternate decision path selected by config,
not an additional condition evaluated alongside stable-basis scanning.

## 18. Design review checklist and current-code ambiguities

The current code has no generic venue-neutral builder intent or finalized
matched-exposure callback yet, and `plan_arb()` is intentionally arbitrage-only.
Those are implementation boundaries to add narrowly; they are not reasons to
change the approved execution or risk architecture. The recommended design
above resolves them by adapting a builder intent into the existing settlement
path and notifying progress by existing `execution_id`.

The following semantics are deliberately fixed in this design so they do not
become implementation-time guesses:

- executable basis is Entropy-vs-hedge in the formulas above;
- progress is conservative common matched gross notional using actual average
  fills;
- unresolved lifecycle is not zero progress;
- builder has one in-flight execution and never overshoots remaining target;
- existing global cooldown is an additional safety floor;
- baseline is captured after strict startup position reconciliation and is not
  corrected or persisted for resume.

No user decision is required before implementation unless the operator wants a
different progress-notional convention or a different interaction with the
existing global cooldown; either would change the explicit semantics above.

