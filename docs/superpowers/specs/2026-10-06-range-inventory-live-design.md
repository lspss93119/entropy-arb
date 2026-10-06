# Range Inventory live adapter design

Status: user approved; implementation only, no deployment or trading.

## Boundaries

Base: `feature/range-inventory-shadow` at
`a81eb822baa1d27ce609c4a5b95fae0a3b383a59`. Work only on the isolated
`feature/range-inventory-live` branch. Production SHA, process, HALT,
config, credentials, lots and history are not to be changed. No merge,
push, deploy, orders, resume or repair of the existing production mismatch.

## Shared causal strategy

Extract the existing shadow signal/target calculation into a pure core.
It accepts completed recorder minutes and actual signed paired inventory,
and emits immutable target/signal metadata. It has no venue clients,
credentials, persistence or execution. Keep the exact percentile ranks,
linear quantiles, time windows, coverage and target formulas.

Frozen settings: long 240 minutes / Q30 entry / Q08 full / Q55 release /
Q93 flat / gamma 0.4; short 120 minutes / Q80 entry / Q95 full / Q08
release / Q02 flat / gamma 1; range 240 minutes Q90-Q10 >= 10 bps;
80% coverage; funding excluded. The gate blocks only increased exposure.
An existing direction releases toward zero, never directly reverses.

Shadow keeps its existing T+1 execution, costs, defaults and CSV schema.
Live is an explicit opt-in `strategy.mode: range_inventory`; fixed and
rolling modes must retain their existing behavior. Range reductions do
not use rolling dynamic thresholds, inventory surcharge or lot BE gates.

## Live planning and safety

The live adapter receives a completed minute, calculating its age from
`minute_ts + 60`, then wakes the existing strategy evaluation immediately.
Use fresh current BBO, not the historical recorder BBO. Signals older than
15 seconds, incomplete/future minutes, duplicate/out-of-order minutes,
invalid data, unavailable coverage or missing state cannot trade.
Shutdown's partial-minute recorder flush cannot create an order.

The first canary profile has $1500 long/short strategy caps and $1500 hard
cap, $53 maximum adjustment per completed minute, and 75% paired visible
BBO depth. Venue position caps and the existing per-order cap, min base,
strategy/venue min notional and common size step remain additional limits.
Use the existing `plan_arb` on one-level books with edge filtering disabled;
Range decides direction, not fixed/rolling premium thresholds. Reductions
remain reduce-only and capped by actual paired inventory. No illegal-size
enlargement, forced dust flatten or opposite-side entry in the same signal.

At most one paired attempt per minute. Persist the consumed signal and
in-flight intent atomically BEFORE submission; a failed write means no
orders. Recheck freshness/HALT/BBO at the final synchronous send boundary.
Reserve the entire attempt budget; partial fills do not allow a same-minute
retry. A restart with an unresolved in-flight intent is fail-closed.

The adapter only produces plans. Existing paired IOC execution, venue
locks, limiter, freshness/last-update protection, residual hedge, API nonce
path, persisted HALT and strict authoritative reconciliation are reused.
An unknown settlement, mismatch or safety-state failure causes HALT, not
guessed state repair. Existing rolling reconciliation math is preserved;
Range supplies its independent expected positions to the same in-flight
known-fill reconciliation mechanism.

## Independent state and telemetry

Store `logs/state/range-<symbol>-<hedge>.json` alongside, but separate from,
the existing lot ledger. Validate market identity, schema/version,
parameter fingerprint, target/direction, paired quantity, latest signal,
consumed minute and in-flight intent. Missing state is valid only with
authoritative positions exactly flat. Never import or modify rolling lots.
Startup, restart and explicit resume validate authoritative positions
against Range state. Any unexplained mismatch persists HALT.

Only actual settled paired quantity advances inventory, including a
successful residual hedge that makes the primary legs paired. Failed or
unresolved hedges cannot falsely advance state. Reductions never mark flat
before the actually closed quantity exhausts inventory. Actual average
prices (including eligible residual fill blending) provide entry cost basis
and realized spread PnL; missing prices make telemetry unavailable, never
fabricated. This accounting does not gate reductions or spend past profits.

Log completed minute, percentiles, range/gate, target/current inventory,
requested adjustment, paired actual fills, BBO depth fraction, signal age
and build/release/cover/blocked action. Persist cumulative realized capture
and missing-price status; funding excluded.

## Verification and rollout blockers

TDD for core extraction, state, planning and engine integration. Retain all
original tests. Run full pytest, compileall, ruff and diff checks.
Pin the actual ANTH historical input/cutoff/hash and compare every original
shadow CSV field with the pre-refactor oracle. Approximate user PnL figures
are not substitutes for identical input/profile parity. Compare the live
adapter's core targets and signal actions minute-by-minute using the same
inventory inputs/profile, with independent T+1 simulated execution.
Immediate live execution is deliberately different from T+1 fills/PnL.

No test authorizes canary deployment. Existing production HALT/mismatch,
read-only authoritative preflight, fresh feeds/coverage, exclusive process
ownership, state migration/flat start and explicit operator authorization
remain blockers to real $1500 trading.
