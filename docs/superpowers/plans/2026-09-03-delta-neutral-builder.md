# Delta-Neutral Builder Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicitly selected `delta_neutral_builder` strategy that adds fixed-size, matched Entropy/hedge exposure toward a fresh session-local target while reusing the existing execution, settlement, hedge, reconciliation, and safety stack.

**Architecture:** Keep strategy decisions and session accounting in a venue-neutral `entropy_arb/builder.py`. Translate an accepted builder decision through a small fixed-size `entropy_arb/execution_intent.py` DTO, then route it through the existing two-leg settlement path rather than `plan_arb()`'s arbitrage-only eligibility rule or a second execution engine. The Engine remains the owner of feed readiness, risk/capacity gates, order submission, slippage, fees, partial fills, hedge/reconcile, lifecycle telemetry, and the single selected strategy; the dashboard consumes a read-only builder snapshot.

**Tech Stack:** Python 3, frozen/slot dataclasses and `Enum`, existing YAML parser, `asyncio`, `OrderBook`, `HLVenue`, `LighterVenue`, current rolling-center implementation, pytest, Ruff, mypy, and the repository's existing fake venues/tests. No new runtime dependency is required.

**Spec:** `docs/superpowers/specs/2026-09-03-delta-neutral-builder-design.md`

## Global Constraints

- Read the approved spec before every implementation session and keep the feature limited to the explicitly selected `delta_neutral_builder` strategy.
- Preserve `stable_basis` and `drifting_basis` decisions, center semantics, inventory surcharge, sizing semantics, execution slippage, fees, reconciliation, lifecycle telemetry, CSV fields, stale-feed fail-closed behavior, and quota-aware websocket behavior.
- Exactly one order-producing strategy is active: `stable_basis`, `drifting_basis`, or `delta_neutral_builder`; there is no concurrent strategy evaluation, automatic diagnosis, selection, or switching.
- The builder domain must not import venue adapters, credentials, signer modules, order APIs, hedge/reconcile code, or network clients; it operates on plain values and explicit result DTOs.
- Do not use `plan_arb()` to decide whether a builder signal is eligible. `plan_arb()` remains unchanged for the legacy arbitrage path; the builder uses the fixed-size intent adapter and the existing common settlement primitives.
- Do not create a second execution or matching engine. Reuse simultaneous two-leg sends, price rounding, `leg_slippage_bps`, fees, partial/one-leg handling, `execution_id`, emergency hedge, reconcile, rate limits, and existing telemetry.
- Use the existing midpoint-to-midpoint premium only for the center. Builder entry uses the direction-specific executable BBO formula from the approved spec and never invents another fee or slippage formula.
- Builder progress is session-local, monotonic, and credited only once after authoritative lifecycle finalization. It is common matched gross notional, not requested size, expected edge, fill edge, P&L, or current-position delta.
- A process restart creates a new builder baseline and zero progress. Do not preload historical execution rows, persist builder session progress, or correct a pre-existing venue imbalance.
- Do not modify rolling-center window, coverage, freshness, median, persistence, or websocket/quota implementations.
- Do not add funding features, market diagnosis, regime detection, quality tiers, maker-first/TWAP behavior, automatic unwind, build horizon, or any other unapproved strategy.
- Tests are deterministic and credential-free. No task sends live orders or requires a wallet/private key.
- Every task follows RED first, minimal GREEN implementation, focused regression, and one logically separated commit. Do not squash the task commits.

## Current-code map and file responsibilities

The current implementation has these relevant seams:

- `entropy_arb/config.py` parses `StrategyConf` and rejects unknown strategy names/parameters. It currently supports `stable_basis` and `drifting_basis`.
- `entropy_arb/strategy.py` owns `StrategyState`, `StableBasisStrategy`, `DriftingBasisStrategy`, rolling-center bootstrap/update/persistence hooks, and `build_strategy()`.
- `entropy_arb/engine.py` creates venues, starts feeds and lifecycle tasks, captures one strategy snapshot in `_scan()`, applies freshness/outage/lock/rate/capacity/inventory gates, calls `plan_arb()`, executes simultaneous legs in `_execute()`, and settles residual hedge through `_maybe_hedge()`/`_note_hedge_result()`.
- `entropy_arb/book.py` contains `OrderBook`, tick helpers, depth walking, and `plan_arb()`. Its fee-clearing arbitrage contract must remain intact.
- `entropy_arb/dashboard.py` renders read-only status, signal, and recent execution panels.
- Existing `tests/test_config.py`, `tests/test_strategy.py`, `tests/test_engine.py`, and `tests/test_dashboard.py` provide the stable/drifting regression baseline and fake venue/settlement fixtures.

The implementation will add only these production files:

| File | Responsibility |
| --- | --- |
| `entropy_arb/builder.py` | Builder direction/status/value objects, executable decision functions, center delegation boundary, session baseline/progress/timers, and pure builder state. No venue/order imports. |
| `entropy_arb/execution_intent.py` | Venue-neutral fixed-size two-leg intent and quantization/leg mapping. It carries BBO references into the existing Engine settlement adapter and never submits orders. |

Existing files modified by later tasks are limited to `entropy_arb/config.py`, `entropy_arb/strategy.py`, `entropy_arb/engine.py`, `entropy_arb/dashboard.py`, and the named regression tests. No other production subsystem is in scope.

The center boundary is intentionally narrow. Existing center strategies keep their current public behavior. A `CenterProvider` protocol/property is added only where needed so the builder composes the existing `StableBasisStrategy` center lifecycle instead of copying rolling history logic. The Engine's center bootstrap/observation helper continues to use the same provider methods and the stable/drifting paths remain byte-for-byte decision-compatible through tests.

---

### Task 1: Builder configuration and pure domain model

**Files:**
- Create: `entropy_arb/builder.py`
- Modify: `entropy_arb/config.py`
- Test: `tests/test_config.py`
- Test: `tests/test_builder.py`

**Interfaces:**
- Consumes: existing YAML `strategy.name`/`strategy.params` validation and rolling-center defaults from `entropy_arb.config`.
- Produces from `entropy_arb.config`:
  ```python
  @dataclass(frozen=True, slots=True)
  class BuilderConf:
      direction: str
      target_additional_notional_usd: float
      chunk_usd: float
      entry_offset_bps: float
      success_cooldown_sec: float
      failed_retry_sec: float
      center_mode: str
      center_bps: float
      center_window_hours: float
      center_update_minutes: int
      center_min_coverage_ratio: float
      center_min_samples: int
      center_last_valid_max_age_hours: float
      center_max_latest_sample_age_sec: float
  ```
  `StrategyConf.builder: BuilderConf | None` is populated only for `delta_neutral_builder`; existing stable/drifting fields remain compatible for their selected strategies.
- Produces from `entropy_arb.builder`:
  ```python
  class BuilderDirection(str, Enum):
      LONG_ENTROPY = "long_entropy"
      SHORT_ENTROPY = "short_entropy"

  class BuilderStatus(str, Enum):
      WAIT_BASIS = "WAIT BASIS"
      READY = "READY"
      EXECUTING = "EXECUTING"
      RETRY = "RETRY"
      COOLDOWN = "COOLDOWN"
      COMPLETE = "COMPLETE / HOLD"

  @dataclass(frozen=True, slots=True)
  class BuilderBBO:
      entropy_bid: float
      entropy_ask: float
      hedge_bid: float
      hedge_ask: float

  @dataclass(frozen=True, slots=True)
  class BuilderDecision:
      enter: bool
      direction: BuilderDirection
      executable_basis_bps: float | None
      trigger_bps: float | None
      advantage_bps: float | None
      reason: str
  ```

- [ ] **Step 1: Write the failing tests**

  Add `tests/test_config.py` cases for a valid builder YAML, both directions, all six builder defaults/values, center fields, unknown builder parameter/name/direction, non-finite or non-positive target/chunk/offset, negative cooldown/retry, and unchanged stable/drifting fixtures. Add `tests/test_builder.py` cases that the enum values and immutable `BuilderBBO`/`BuilderDecision` values are constructed without importing any venue module.

  ```python
  def test_delta_neutral_builder_config_is_strict_and_explicit():
      cfg = load("""
      strategy:
        name: delta_neutral_builder
        params:
          direction: long_entropy
          target_additional_notional_usd: 5000
          chunk_usd: 100
          entry_offset_bps: 1.0
          success_cooldown_sec: 60
          failed_retry_sec: 5
          center_mode: fixed
          center_bps: -1.8
      """)
      assert cfg.strategy.builder is not None
      assert cfg.strategy.builder.direction == "long_entropy"
      assert cfg.strategy.builder.target_additional_notional_usd == 5000
  ```

- [ ] **Step 2: Run the RED tests**

  Run:
  ```bash
  python3 -m pytest tests/test_config.py::test_delta_neutral_builder_config_is_strict_and_explicit tests/test_builder.py -q
  ```
  Expected: collection or assertion failure because the builder strategy/schema and `tests/test_builder.py` domain types do not yet exist.

- [ ] **Step 3: Write the minimal implementation**

  Add `BuilderConf` and make `StrategyConf` hold an optional builder configuration without removing fields used by stable/drifting. Extend `_parse_strategy()` with an explicit `delta_neutral_builder` parameter allow-list. Require `direction`, `target_additional_notional_usd`, `chunk_usd`, `entry_offset_bps`, and `center_bps`; default `center_mode` to `fixed`, `success_cooldown_sec` to `60`, `failed_retry_sec` to `5`, and reuse the existing rolling-center availability defaults. Reject unknown keys, builder `upper_bps`/`lower_bps`, and invalid finite/positive/non-negative values. Keep the legacy strategy error behavior unchanged. Add the immutable domain enums/value objects exactly as declared; do not evaluate basis or touch the Engine in this task.

- [ ] **Step 4: Run the GREEN and regression tests**

  Run:
  ```bash
  python3 -m pytest tests/test_config.py tests/test_builder.py -q
  python3 -m pytest tests/test_strategy.py tests/test_engine.py -q
  ```
  Expected: all builder/config tests pass and stable/drifting strategy/engine regressions remain green.

- [ ] **Step 5: Commit**

  ```bash
  git add entropy_arb/config.py entropy_arb/builder.py tests/test_config.py tests/test_builder.py
  git commit -m "feat: add delta-neutral builder config model"
  ```

### Task 2: Executable BBO basis and entry decision

**Files:**
- Modify: `entropy_arb/builder.py`
- Test: `tests/test_builder.py`

**Interfaces:**
- Consumes: `BuilderDirection`, `BuilderBBO`, and `BuilderDecision` from Task 1.
- Produces:
  ```python
  def executable_basis_bps(
      direction: BuilderDirection, bbo: BuilderBBO
  ) -> float: ...

  def entry_trigger_bps(
      direction: BuilderDirection,
      center_bps: float,
      entry_offset_bps: float,
  ) -> float: ...

  def evaluate_builder_signal(
      direction: BuilderDirection,
      bbo: BuilderBBO,
      *,
      center_bps: float,
      entry_offset_bps: float,
  ) -> BuilderDecision: ...
  ```
  The decision contains no quantity, fee, inventory, venue object, or network behavior; sizing is a later boundary.

- [ ] **Step 1: Write the failing tests**

  Add deterministic tests for the exact long/short formulas, inclusive trigger boundaries, just-inside/just-outside rejection, symmetric fixture construction, invalid/non-finite prices, center, and offset inputs, and proof that midpoint-only values do not drive the decision.

  ```python
  def test_long_uses_entropy_ask_and_hedge_bid_at_inclusive_boundary():
      bbo = BuilderBBO(entropy_bid=99.0, entropy_ask=101.0,
                       hedge_bid=102.0, hedge_ask=103.0)
      basis = executable_basis_bps(BuilderDirection.LONG_ENTROPY, bbo)
      exact_offset = -basis
      decision = evaluate_builder_signal(
          BuilderDirection.LONG_ENTROPY, bbo,
          center_bps=0.0, entry_offset_bps=exact_offset,
      )
      assert basis == pytest.approx((101.0 / 102.0 - 1) * 1e4)
      assert decision.enter is True
  ```

- [ ] **Step 2: Run the RED tests**

  Run:
  ```bash
  python3 -m pytest tests/test_builder.py -k "basis or trigger or midpoint" -q
  ```
  Expected: failure because the pure basis/trigger functions are not defined.

- [ ] **Step 3: Write the minimal implementation**

  Validate finite positive BBO prices and positive finite offset. Implement exactly:
  ```text
  long_entropy  = (entropy_ask / hedge_bid - 1) * 10000
  short_entropy = (entropy_bid / hedge_ask - 1) * 10000
  long trigger  = center_bps - entry_offset_bps
  short trigger = center_bps + entry_offset_bps
  long enters   = executable <= trigger
  short enters  = executable >= trigger
  ```
  Set `advantage_bps` to the signed distance in the direction of the trigger and return a reason such as `basis_favorable` or `basis_unfavorable`. Do not call `calculate_premiums()` for the trigger and do not apply fees, inventory surcharge, or order sizing here.

- [ ] **Step 4: Run the GREEN and regression tests**

  Run:
  ```bash
  python3 -m pytest tests/test_builder.py -q
  python3 -m pytest tests/test_premium.py tests/test_strategy.py tests/test_engine.py -q
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add entropy_arb/builder.py tests/test_builder.py
  git commit -m "feat: add builder executable basis decisions"
  ```

### Task 3: Fixed-size venue-neutral execution intent

**Files:**
- Create: `entropy_arb/execution_intent.py`
- Test: `tests/test_execution_intent.py`

**Interfaces:**
- Consumes: `BuilderDirection` and `BuilderBBO` from `entropy_arb.builder`; Engine-provided size step/minimum values.
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class FixedSizeExecutionIntent:
      strategy_name: str
      direction: BuilderDirection
      buy_venue_key: str
      sell_venue_key: str
      qty: float
      buy_reference_px: float
      sell_reference_px: float
      requested_notional_usd: float
      buy_notional_usd: float
      sell_notional_usd: float

  def build_fixed_size_intent(
      direction: BuilderDirection,
      bbo: BuilderBBO,
      desired_notional_usd: float,
      *,
      size_step: float,
      min_base: float,
      min_order_notional: float,
  ) -> FixedSizeExecutionIntent | None: ...
  ```
  The adapter maps long to BUY Entropy/SELL hedge and short to BUY hedge/SELL Entropy. It carries raw BBO references only; it does not pre-apply slippage, fees, or submit anything.

- [ ] **Step 1: Write the failing tests**

  Add tests for both direction mappings, fixed `$100` request, tick quantization, minimum-base/notional rejection, non-positive/invalid input rejection, and no intentional overshoot. Assert `strategy_name == "delta_neutral_builder"` and that no venue import is needed.

  ```python
  def test_long_intent_is_buy_entropy_sell_hedge_and_is_fixed_size():
      intent = build_fixed_size_intent(
          BuilderDirection.LONG_ENTROPY,
          BuilderBBO(99.0, 101.0, 98.0, 100.0),
          100.0, size_step=0.01, min_base=0.01,
          min_order_notional=10.0,
      )
      assert intent is not None
      assert (intent.buy_venue_key, intent.sell_venue_key) == ("entropy", "hedge")
      assert intent.buy_notional_usd <= 100.0
  ```

- [ ] **Step 2: Run the RED tests**

  Run:
  ```bash
  python3 -m pytest tests/test_execution_intent.py -q
  ```
  Expected: collection failure because `entropy_arb.execution_intent` does not yet exist.

- [ ] **Step 3: Write the minimal implementation**

  Select the executable reference prices from the direction formula. Use `qty = floor_step(desired_notional_usd / max(buy_reference_px, sell_reference_px), size_step)` so neither entry leg intentionally exceeds the requested USD chunk; reject quantities below `min_base` or either leg below `min_order_notional`. Preserve raw references and calculated per-leg notionals in the frozen DTO. Do not create an `ArbPlan`, call `plan_arb()`, call a venue, or calculate slippage.

- [ ] **Step 4: Run the GREEN and regression tests**

  Run:
  ```bash
  python3 -m pytest tests/test_execution_intent.py tests/test_book.py -q
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add entropy_arb/execution_intent.py tests/test_execution_intent.py
  git commit -m "feat: add fixed-size builder execution intent"
  ```

### Task 4: Session baseline and finalized matched-progress accounting

**Files:**
- Modify: `entropy_arb/builder.py`
- Test: `tests/test_builder.py`

**Interfaces:**
- Consumes: `BuilderDirection`, `BuilderStatus`, `BuilderConf`, and the fixed-size intent's execution identifier at the Engine boundary.
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class BuilderPositionSnapshot:
      quantity: float
      notional_usd: float | None

  @dataclass(frozen=True, slots=True)
  class BuilderBaseline:
      entropy: BuilderPositionSnapshot
      hedge: BuilderPositionSnapshot

  @dataclass(frozen=True, slots=True)
  class BuilderExecutionResult:
      execution_id: str
      finalized: bool
      lifecycle_status: str
      entropy_entry_filled_qty: float
      hedge_entry_filled_qty: float
      entropy_entry_avg_px: float | None
      hedge_entry_avg_px: float | None

  def finalized_matched_notional_usd(
      result: BuilderExecutionResult,
  ) -> float | None: ...

  @dataclass(frozen=True, slots=True)
  class BuilderProgressUpdate:
      execution_id: str
      matched_notional_usd: float | None
      built_before_usd: float
      built_after_usd: float
      applied: bool
      unresolved: bool

  class BuilderSession:
      def __init__(
          self,
          *,
          direction: BuilderDirection,
          target_additional_notional_usd: float,
          chunk_usd: float,
          success_cooldown_sec: float,
          failed_retry_sec: float,
      ) -> None: ...

      def begin(self, baseline: BuilderBaseline, *, now: float) -> None: ...
  ```
  `finalized_matched_notional_usd()` returns `None` for non-final results or a positive matched quantity with an unknown actual average price; otherwise it returns `min(entropy_qty, hedge_qty) * min(entropy_avg_px, hedge_avg_px)`, with zero for no common matched quantity. `BuilderSession` records a new baseline and applies each execution ID once.

- [ ] **Step 1: Write the failing tests**

  Add tests for a fresh baseline with an existing imbalance, restart creating a different zero-progress session, full/full progress, partial progress, one-leg plus hedge-away represented by zero common entry match, missing actual average price remaining unresolved, failed execution not counting requested quantity, and duplicate final events being idempotent.

  Define test-only helpers with these concrete signatures so each lifecycle case is explicit:
  ```python
  def configured_session() -> BuilderSession: ...
  def zero_match_result(execution_id: str) -> BuilderExecutionResult: ...
  ```

  ```python
  def test_partial_and_one_leg_progress_use_final_actual_match_only():
      session = BuilderSession(
          direction=BuilderDirection.LONG_ENTROPY,
          target_additional_notional_usd=5000.0,
          chunk_usd=100.0,
          success_cooldown_sec=60.0,
          failed_retry_sec=5.0,
      )
      session.begin(BuilderBaseline(
          entropy=BuilderPositionSnapshot(10.0, 1000.0),
          hedge=BuilderPositionSnapshot(-7.0, -700.0),
      ), now=1000.0)
      update = session.apply_finalized(BuilderExecutionResult(
          execution_id="e1", finalized=True, lifecycle_status="hedged",
          entropy_entry_filled_qty=0.4, hedge_entry_filled_qty=0.2,
          entropy_entry_avg_px=100.0, hedge_entry_avg_px=99.0,
      ), now=1001.0)
      assert update.matched_notional_usd == pytest.approx(19.8)
      assert session.built_matched_notional_usd == pytest.approx(19.8)
  ```

- [ ] **Step 2: Run the RED tests**

  Run:
  ```bash
  python3 -m pytest tests/test_builder.py -k "baseline or progress or matched or restart" -q
  ```
  Expected: failure because `BuilderSession` and finalized-result accounting do not yet exist.

- [ ] **Step 3: Write the minimal implementation**

  Add `BuilderSession` with `begin(baseline, now)`, zero initial progress, local session identity, and a set/map of applied execution IDs. Require `finalized=True` before applying. Use only actual entry fills and actual average prices; never substitute requested limits. A one-leg fill that is later hedged away is passed as zero common entry match and adds zero. Keep current positions separate from baseline/progress. Do not read files, CSV, storage, or external state.

- [ ] **Step 4: Run the GREEN and regression tests**

  Run:
  ```bash
  python3 -m pytest tests/test_builder.py -q
  python3 -m pytest tests/test_engine.py tests/test_dashboard.py -q
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add entropy_arb/builder.py tests/test_builder.py
  git commit -m "feat: track builder session progress"
  ```

### Task 5: Finalized execution-result callback integration

**Files:**
- Modify: `entropy_arb/engine.py`
- Modify: `entropy_arb/builder.py`
- Test: `tests/test_engine.py`
- Test: `tests/test_builder.py`

**Interfaces:**
- Consumes: `FixedSizeExecutionIntent`, `BuilderExecutionResult`, and `BuilderSession` from Tasks 3–4; existing `execution_id`, `_execute()`, `_maybe_hedge()`, `_note_hedge_result()`, and `_execution_contexts`.
- Produces:
  ```python
  class BuilderSettlementSink(Protocol):
      def on_builder_execution_finalized(
          self, result: BuilderExecutionResult, now: float,
      ) -> BuilderProgressUpdate: ...
  ```
  Engine invokes the sink only after existing two-leg/hedge lifecycle settlement is authoritative. Full/full finalizes immediately; residual partial/one-leg finalizes only after the existing hedge/reconcile path has a final result. Unresolved or unknown average-price outcomes do not become zero and do not notify progress as failed.

- [ ] **Step 1: Write the failing tests**

  Extend the existing fake `SettlementVenue` tests to submit a builder intent through the common Engine settlement adapter and assert that the same `execution_id` reaches the builder session after full/full, partial plus residual hedge, one-leg plus hedge-away, and unresolved hedge. Assert existing `actual_usd`, `fill_edge`, `total_fill_edge`, CSV fields, and hedge telemetry are unchanged for stable executions.

  Define test-only helpers with concrete signatures:
  ```python
  def make_builder_engine_with_settlement_venue(
      *, buy_response: dict, sell_response: dict, hedge_response: dict | None = None,
  ) -> Engine: ...
  def make_test_intent() -> FixedSizeExecutionIntent: ...
  async def settle_pending_builder_lifecycle(
      eng: Engine, execution_id: str,
  ) -> None: ...
  ```

  ```python
  async def test_builder_progress_callback_waits_for_hedge_finalization():
      eng = make_builder_engine_with_settlement_venue(
          buy_response=settlement_info("filled", 1.0, 100.0),
          sell_response=settlement_info("canceled", 0.0),
          hedge_response=settlement_info("filled", 1.0, 98.0),
      )
      execution_id = await eng._execute_builder_intent(make_test_intent())
      assert eng.builder_session.built_matched_notional_usd == 0.0
      await settle_pending_builder_lifecycle(eng, execution_id)
      assert eng.builder_session.built_matched_notional_usd == 0.0
  ```

- [ ] **Step 2: Run the RED tests**

  Run:
  ```bash
  python3 -m pytest tests/test_engine.py -k "builder_settlement or builder_progress" -q
  ```
  Expected: failure because Engine has no builder intent dispatch or finalized matched-progress sink.

- [ ] **Step 3: Write the minimal implementation**

  Split only the common leg-send/settlement mechanics needed by the builder from the existing `_execute()` without changing the stable `ArbPlan` path's inputs or outputs. Add an Engine adapter that maps a `FixedSizeExecutionIntent` to concrete venue objects and raw BBO limits, then calls the same slippage, send, fill accounting, `_maybe_hedge()`, `_note_hedge_result()`, reconcile escalation, and telemetry code. Preserve `fill_edge` as matched two-leg quality and do not put builder entry quality into `total_fill_edge` or claim reward/P&L. Emit `BuilderExecutionResult` once per `execution_id` only after the existing lifecycle knows the final common match; retain pending contexts when unresolved.

- [ ] **Step 4: Run the GREEN and regression tests**

  Run:
  ```bash
  python3 -m pytest tests/test_engine.py tests/test_builder.py -q
  python3 -m pytest tests/test_premium.py tests/test_recorder.py tests/test_dashboard.py -q
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add entropy_arb/engine.py entropy_arb/builder.py tests/test_engine.py tests/test_builder.py
  git commit -m "feat: return finalized builder execution results"
  ```

### Task 6: Builder cooldown and retry state machine

**Files:**
- Modify: `entropy_arb/builder.py`
- Test: `tests/test_builder.py`

**Interfaces:**
- Consumes: `BuilderStatus`, `BuilderProgressUpdate`, configured success/failure delays, and a deterministic `now` from Task 4.
- Produces:
  ```python
  class BuilderSession:
      def can_enter(self, now: float) -> bool: ...
      def mark_execution_started(self, execution_id: str, now: float) -> None: ...
      def apply_finalized(
          self, result: BuilderExecutionResult, now: float,
      ) -> BuilderProgressUpdate: ...
      def status(self, now: float) -> BuilderStatus: ...
      def retry_remaining_sec(self, now: float) -> float: ...
      def cooldown_remaining_sec(self, now: float) -> float: ...

      def status_snapshot(
          self,
          *,
          now: float,
          center: StrategyState,
          executable_basis_bps: float | None,
          current_entropy: BuilderPositionSnapshot,
          current_hedge: BuilderPositionSnapshot,
          reason: str | None,
      ) -> BuilderStatusSnapshot: ...

  @dataclass(frozen=True, slots=True)
  class BuilderStatusSnapshot:
      status: BuilderStatus
      direction: BuilderDirection
      target_additional_notional_usd: float
      built_matched_notional_usd: float
      remaining_target_usd: float
      baseline_entropy: BuilderPositionSnapshot
      baseline_hedge: BuilderPositionSnapshot
      current_entropy: BuilderPositionSnapshot
      current_hedge: BuilderPositionSnapshot
      center_bps: float | None
      center_source: str | None
      executable_basis_bps: float | None
      entry_trigger_bps: float | None
      entry_advantage_bps: float | None
      chunk_usd: float
      reason: str | None
  ```
  Unresolved lifecycle remains `EXECUTING`; nonzero final match starts `success_cooldown_sec`, zero final match starts `failed_retry_sec`; completion takes precedence over timers.

- [ ] **Step 1: Write the failing tests**

  Use a fake numeric clock and test startup `WAIT BASIS`, READY-to-EXECUTING, unresolved no-timer behavior, zero-progress `RETRY` for exactly five seconds, nonzero progress `COOLDOWN` for exactly sixty seconds, one in-flight guard, timer expiry, and `COMPLETE / HOLD` after target.

  ```python
  def test_zero_progress_retries_without_success_cooldown():
      session = configured_session()
      session.mark_execution_started("e0", now=10.0)
      session.apply_finalized(zero_match_result("e0"), now=11.0)
      assert session.status(11.0) is BuilderStatus.RETRY
      assert session.retry_remaining_sec(11.0) == pytest.approx(5.0)
      assert session.can_enter(15.99) is False
      assert session.can_enter(16.0) is True
  ```

- [ ] **Step 2: Run the RED tests**

  Run:
  ```bash
  python3 -m pytest tests/test_builder.py -k "cooldown or retry or executing or complete" -q
  ```
  Expected: failure because timer/status transitions are not implemented.

- [ ] **Step 3: Write the minimal implementation**

  Store only monotonic in-memory timer deadlines and one in-flight execution ID. Do not use `asyncio.sleep()` in the strategy. Let Engine's existing global `cfg.cooldown_sec` remain an outer safety floor; the Engine combines it with builder readiness using the maximum delay. No timer starts for pending/unresolved settlement.

- [ ] **Step 4: Run the GREEN and regression tests**

  Run:
  ```bash
  python3 -m pytest tests/test_builder.py -q
  python3 -m pytest tests/test_engine.py -q
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add entropy_arb/builder.py tests/test_builder.py
  git commit -m "feat: add builder cooldown and retry state"
  ```

### Task 7: Target, minimum, cap, and risk clamping

**Files:**
- Modify: `entropy_arb/builder.py`
- Modify: `entropy_arb/execution_intent.py`
- Test: `tests/test_builder.py`
- Test: `tests/test_execution_intent.py`

**Interfaces:**
- Consumes: session remaining target, configured chunk/min/max values, venue headroom, existing Engine risk/capacity values, and the fixed-size intent builder.
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class BuilderCapacity:
      remaining_target_usd: float
      entropy_headroom_usd: float
      hedge_headroom_usd: float
      max_order_notional_usd: float
      min_order_notional_usd: float
      risk_headroom_usd: float | None = None

  @dataclass(frozen=True, slots=True)
  class BuilderSizeDecision:
      requested_chunk_usd: float
      clamped_notional_usd: float
      reason: str
      tradable: bool

  def clamp_builder_chunk(
      chunk_usd: float, capacity: BuilderCapacity,
  ) -> BuilderSizeDecision: ...
  ```
  Reasons include `ok`, `TARGET_REMAINDER_BELOW_MINIMUM`, and `CAPACITY_BLOCKED`. The function never changes the target or bypasses Engine gates.

- [ ] **Step 1: Write the failing tests**

  Test chunk `$100` by default, remaining target `$40` clamps to `$40` only when tradable, remaining `$4` with a `$10` minimum produces no intent and a visible `TARGET_REMAINDER_BELOW_MINIMUM` hold reason, caps below target produce `CAPACITY_BLOCKED`, risk headroom is honored, and no result exceeds remaining target. Test size-step flooring after USD clamp.

- [ ] **Step 2: Run the RED tests**

  Run:
  ```bash
  python3 -m pytest tests/test_builder.py tests/test_execution_intent.py -k "clamp or minimum or cap or overshoot" -q
  ```
  Expected: failure because capacity DTOs and clamp function do not yet exist.

- [ ] **Step 3: Write the minimal implementation**

  Clamp with the minimum of positive remaining target, configured chunk, existing maximum order notional, both venue headrooms, and optional risk headroom. If the positive remainder/capacity is below the effective minimum, return `tradable=False` with the specified reason rather than overshooting or marking the session complete. Pass only a positive clamped value to `build_fixed_size_intent()`; keep precision/minimum enforcement there and in existing Engine gates. Mark a cap-blocked target as a non-trading `WAIT BASIS`/hold state while preserving remaining target.

- [ ] **Step 4: Run the GREEN and regression tests**

  Run:
  ```bash
  python3 -m pytest tests/test_builder.py tests/test_execution_intent.py -q
  python3 -m pytest tests/test_book.py tests/test_engine.py -q
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add entropy_arb/builder.py entropy_arb/execution_intent.py tests/test_builder.py tests/test_execution_intent.py
  git commit -m "feat: clamp builder target and risk capacity"
  ```

### Task 8: Explicit inventory-surcharge separation

**Files:**
- Modify: `entropy_arb/engine.py`
- Test: `tests/test_builder.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `BuilderDecision`/`BuilderBBO` from Tasks 1–2 and the existing `_inv_add_bps()`/`_eff_threshold()` contract.
- Produces:
  ```python
  def _builder_signal(
      self, bbo: BuilderBBO, center_state: StrategyState,
  ) -> BuilderDecision: ...
  ```
  The helper is a narrow Engine-to-domain adapter: it passes only the captured center and builder offset to `evaluate_builder_signal()`. It never calls `_eff_threshold()` or `_inv_add_bps()`; the stable path continues to call both exactly as before.

- [ ] **Step 1: Write the failing tests**

  Add an Engine adapter test that monkeypatches `_inv_add_bps()` to raise if called and invokes `_builder_signal()` with a favorable BBO; assert the pure decision is accepted. Keep the existing stable inventory ladder and hurdle tests and add an explicit stable assertion that the surcharge changes the stable hurdle by the same amount as before. Also retain the pure function test showing no inventory argument is required.

- [ ] **Step 2: Run the RED tests**

  Run:
  ```bash
  python3 -m pytest tests/test_builder.py tests/test_engine.py -k "builder_signal or inventory_ladder or eff_threshold" -q
  ```
  Expected: collection or attribute failure because `_builder_signal()` is not present; existing stable inventory/hurdle tests remain green.

- [ ] **Step 3: Write the minimal implementation**

  Keep `_inv_add_bps()` and `_eff_threshold()` untouched for `stable_basis`. Add only `_builder_signal()` and keep its call to `evaluate_builder_signal()` free of inventory inputs; Task 9 will route builder admission through this helper and common safety/capacity checks without adding the surcharge. Do not alter Engine order dispatch, inventory positions, caps, or sizing calculations in this task.

- [ ] **Step 4: Run the GREEN and regression tests**

  Run:
  ```bash
  python3 -m pytest tests/test_engine.py tests/test_builder.py -q
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add entropy_arb/engine.py tests/test_builder.py tests/test_engine.py
  git commit -m "feat: separate builder inventory surcharge"
  ```

### Task 9: Strategy factory, session startup, and Engine dispatch

**Files:**
- Modify: `entropy_arb/config.py`
- Modify: `entropy_arb/strategy.py`
- Modify: `entropy_arb/engine.py`
- Test: `tests/test_config.py`
- Test: `tests/test_strategy.py`
- Test: `tests/test_engine.py`
- Test: `tests/test_builder.py`

**Interfaces:**
- Consumes: `StrategyConf.builder`, `DeltaNeutralBuilderStrategy`, `FixedSizeExecutionIntent`, `BuilderSession`, and existing venue/feed lifecycle.
- Produces:
  ```python
  class CenterProvider(Protocol):
      center_mode: str
      fixed_center_bps: float
      center_window_hours: float
      center_update_minutes: int
      center_min_coverage_ratio: float
      center_min_samples: int
      center_last_valid_max_age_hours: float
      center_max_latest_sample_age_sec: float
      window_sec: float
      requires_observations: bool
      def state(self) -> StrategyState: ...
      def bootstrap(self, observations, *, now: float) -> RollingCenterUpdate | None: ...
      def update(self, timestamp: float, premium_bps: float) -> RollingCenterUpdate | None: ...
      def restore_last_valid(self, snapshot: RollingCenterSnapshot | None, *, now: float) -> bool: ...
      def last_valid_snapshot(self) -> RollingCenterSnapshot | None: ...
      def latest_sample_age_sec(self, timestamp: float) -> float | None: ...
      def rolling_coverage_summary(self, *, now: float) -> tuple[int, float, float]: ...

  class DeltaNeutralBuilderStrategy:
      name = "delta_neutral_builder"
      center_provider: CenterProvider
      @property
      def requires_observations(self) -> bool: ...
      def __init__(
          self,
          conf: BuilderConf,
          center_provider: CenterProvider,
      ) -> None: ...
      def state(self) -> StrategyState: ...
      def center_state(self) -> StrategyState: ...
      def decide(self, bbo: BuilderBBO, *, now: float) -> BuilderDecision: ...
      def status_snapshot(
          self,
          *,
          now: float,
          current_entropy: BuilderPositionSnapshot,
          current_hedge: BuilderPositionSnapshot,
      ) -> BuilderStatusSnapshot: ...
  ```
  `DeltaNeutralBuilderStrategy` composes an existing `StableBasisStrategy` as its center provider; it proxies center bootstrap/update/last-valid/coverage methods rather than copying rolling semantics. Its `state()` is a center-lifecycle compatibility view only; builder dashboard/status never renders stable threshold bands from it. `build_strategy()` returns exactly the configured strategy and raises for unknown names. Engine uses a small dispatcher (`_scan_builder()` vs existing legacy `_scan()` path), captures baseline only after strict startup reconciliation, and never runs two entry paths.

- [ ] **Step 1: Write the failing tests**

  Add factory/config tests for builder selection and exact mutual exclusion, startup baseline capture after strict position reconciliation, fresh session progress zero, new-session restart semantics, existing imbalance not generating a correction, record-only creating no live builder session/observer/orders, and stable/drifting factory regressions. Add Engine tests that a favorable long/short BBO produces only the configured direction and one in-flight intent.

  Define a test-only `builder_yaml() -> str` helper containing the complete approved `strategy: name: delta_neutral_builder` mapping and minimal execution/recorder sections.

  ```python
  def test_factory_selects_builder_without_starting_stable_path():
      cfg = load(builder_yaml())
      strategy = build_strategy(cfg.strategy)
      assert strategy.name == "delta_neutral_builder"
      assert strategy.center_state().center_bps == pytest.approx(-1.8)
  ```

- [ ] **Step 2: Run the RED tests**

  Run:
  ```bash
  python3 -m pytest tests/test_config.py tests/test_strategy.py tests/test_engine.py tests/test_builder.py -k "builder or factory or baseline or record_only" -q
  ```
  Expected: failure because factory/Engine builder dispatch and startup session wiring do not yet exist.

- [ ] **Step 3: Write the minimal implementation**

  Extend `build_strategy()` with an explicit builder branch and no fallback to stable/drifting. Add `CenterProvider` only as an adapter contract; leave current stable/drifting methods and values unchanged. In Engine, use `getattr(strategy, "center_provider", strategy)` only for the existing center bootstrap/observation lifecycle, so the builder's center uses the same fixed/rolling/last-valid source and `calculate_premiums()` behavior. After strict `_reconcile_positions(hedge=False, strict=True)`, snapshot each venue's signed quantity and own-price signed notional as `BuilderBaseline`; do not compare the two baselines to create an order. Add a single strategy dispatcher: legacy stable/drifting continue through the existing `plan_arb()` path, while builder checks common fresh/readiness/outage/lock/rate/halt gates, computes executable BBO decision, clamps capacity, creates one fixed intent, and marks it in flight. `record_only` skips builder session, observer, and execution exactly as it skips live strategy today.

- [ ] **Step 4: Run the GREEN and regression tests**

  Run:
  ```bash
  python3 -m pytest tests/test_config.py tests/test_strategy.py tests/test_engine.py tests/test_builder.py -q
  python3 -m pytest tests/test_quota_reconnect.py tests/test_ws_lifecycle.py tests/test_rolling_center_availability.py -q
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add entropy_arb/config.py entropy_arb/strategy.py entropy_arb/engine.py tests/test_config.py tests/test_strategy.py tests/test_engine.py tests/test_builder.py
  git commit -m "feat: select delta-neutral builder strategy"
  ```

### Task 10: Builder dashboard and status observability

**Files:**
- Modify: `entropy_arb/engine.py`
- Modify: `entropy_arb/dashboard.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `BuilderStatusSnapshot` from `entropy_arb.builder`, Engine current positions, baseline, center source, executable basis, trigger, advantage, capacity reason, and timer methods.
- Produces a read-only builder panel containing:
  ```text
  Direction       LONG Entropy / SHORT Lighter
  Target          $... additional / side
  Built           $...
  Progress        ...%
  Baseline        Entropy ... / Lighter ...
  Current         Entropy ... / Lighter ...
  Center          ... bps
  Center source   fresh_rolling | last_valid | fixed_fallback
  Executable      ... bps
  Entry trigger   <= ... / >= ... bps
  Entry advantage ... bps
  Chunk           $...
  Status          WAIT BASIS | READY | EXECUTING | RETRY Ns | COOLDOWN Ns | COMPLETE / HOLD
  ```
  Stable/drifting signal and status rendering stays unchanged. The status/dashboard path calls only snapshot/state accessors; it never calls `update()`, starts timers, submits orders, or mutates session state.

- [ ] **Step 1: Write the failing tests**

  Add Rich render tests using the existing fake Engine for long and short builder states, center sources (`fresh_rolling`, `last_valid`, `fixed_fallback`), each timer/status, baseline versus current positions, target/progress, and trigger orientation. Keep existing stable dashboard assertions unchanged and add a test that rendering does not alter the builder state or update count.

- [ ] **Step 2: Run the RED tests**

  Run:
  ```bash
  python3 -m pytest tests/test_dashboard.py tests/test_engine.py -k "builder or status_snapshot" -q
  ```
  Expected: failure because dashboard/status has no builder panel or builder snapshot branch.

- [ ] **Step 3: Write the minimal implementation**

  Add a builder-only branch in the existing signal/status renderers. Format direction-specific trigger signs and timer text from the immutable snapshot. Keep stable/drifting `_status_strategy_desc`, `_signal_panel`, and current execution semantics intact. Do not calculate progress from current position minus baseline; read the session's credited value.

- [ ] **Step 4: Run the GREEN and regression tests**

  Run:
  ```bash
  python3 -m pytest tests/test_dashboard.py tests/test_engine.py -q
  python3 -m pytest tests/test_config.py tests/test_strategy.py tests/test_recorder.py -q
  ```

- [ ] **Step 5: Commit**

  ```bash
  git add entropy_arb/engine.py entropy_arb/dashboard.py tests/test_dashboard.py tests/test_engine.py
  git commit -m "feat: expose builder status"
  ```

### Task 11: Full builder integration regression and verification gate

**Files:**
- Test: `tests/test_builder.py`
- Test: `tests/test_execution_intent.py`
- Test: `tests/test_config.py`
- Test: `tests/test_strategy.py`
- Test: `tests/test_engine.py`
- Test: `tests/test_dashboard.py`
- Test: `tests/test_quota_reconnect.py` (regression only; modify only if a narrowly scoped assertion is required)

**Interfaces:**
- Consumes: all production interfaces from Tasks 1–10.
- Produces: no new production API. This task locks the approved test matrix and records the final verification evidence; any production edit is allowed only if a test exposes a concrete implementation defect and must be committed separately with its exact cause.

- [ ] **Step 1: Write the failing integration/regression tests**

  Add or complete named tests covering every approved matrix item:

  | Spec requirement | Test coverage |
  | --- | --- |
  | stable unchanged; mutual exclusion; invalid name/direction/parameter | `test_stable_path_unchanged`, `test_only_selected_strategy_dispatches`, `test_builder_config_rejects_unknown_values` |
  | long/short mapping and executable ask/bid formulas | `test_long_direction_mapping`, `test_short_direction_mapping`, `test_long_uses_ask_bid`, `test_short_uses_bid_ask` |
  | inclusive long/short triggers; midpoint cannot substitute | `test_long_trigger_boundary`, `test_short_trigger_boundary`, `test_midpoint_basis_alone_does_not_enter` |
  | fixed/rolling/last-valid center sources; no builder inventory surcharge; stable surcharge retained | `test_builder_accepts_center_sources`, `test_builder_skips_inventory_surcharge`, `test_stable_inventory_surcharge_regression` |
  | fixed chunk, remaining/cap/risk/min/precision clamps, no overshoot, stale/empty rejection | `test_default_chunk`, `test_remaining_target_clamp`, `test_caps_and_risk_clamp`, `test_minimum_remainder_holds`, `test_no_target_overshoot`, `test_stale_or_empty_books_block` |
  | full/partial/hedge-away/unresolved progress and one application per execution ID | `test_full_match_progress`, `test_partial_match_progress`, `test_hedge_away_zero_progress`, `test_unresolved_is_not_zero`, `test_execution_id_idempotency` |
  | retry/cooldown/completion | `test_zero_progress_retry`, `test_success_cooldown`, `test_pending_has_no_timer`, `test_completion_holds` |
  | restart baseline and imbalance preservation | `test_restart_new_baseline`, `test_existing_imbalance_is_observational` |
  | execution/hedge/reconcile, STALE, quota behavior | `test_builder_reuses_existing_settlement`, `test_builder_stale_fail_closed`, existing `tests/test_quota_reconnect.py` and `tests/test_ws_lifecycle.py` |
  | dashboard baseline/current/center/basis/progress/status and read-only behavior | `test_builder_dashboard_panel`, `test_dashboard_is_read_only` |

- [ ] **Step 2: Run the RED integration tests**

  Run:
  ```bash
  python3 -m pytest tests/test_builder.py tests/test_execution_intent.py tests/test_engine.py tests/test_dashboard.py -q
  ```
  Expected: any remaining integration seam is identified by a focused failure; do not weaken an approved assertion to make it pass.

- [ ] **Step 3: Write only minimal integration corrections**

  Resolve concrete test failures without changing the approved formulas, center semantics, execution behavior, or out-of-scope systems. Keep any production correction in the smallest existing boundary and preserve the stable path's exact plan/settlement/telemetry behavior.

- [ ] **Step 4: Run the complete verification gate**

  Run all commands and retain their actual counts/output:
  ```bash
  python3 -m pytest tests/test_builder.py tests/test_execution_intent.py tests/test_config.py tests/test_strategy.py -q
  python3 -m pytest tests/test_engine.py tests/test_book.py tests/test_premium.py -q
  python3 -m pytest tests/test_recorder.py tests/test_reference.py tests/test_quota_reconnect.py tests/test_ws_lifecycle.py -q
  python3 -m pytest tests/test_dashboard.py -q
  python3 -m pytest -q
  python3 -m pytest -q -p no:cacheprovider
  ruff check entropy_arb/builder.py entropy_arb/execution_intent.py entropy_arb/config.py entropy_arb/strategy.py entropy_arb/engine.py entropy_arb/dashboard.py tests/test_builder.py tests/test_execution_intent.py tests/test_config.py tests/test_strategy.py tests/test_engine.py tests/test_dashboard.py
  python3 -m mypy entropy_arb/builder.py entropy_arb/execution_intent.py entropy_arb/config.py entropy_arb/strategy.py entropy_arb/engine.py entropy_arb/dashboard.py
  git diff --check
  ```
  Confirm no wallet/private key is loaded by tests and no live order endpoint is called.

- [ ] **Step 5: Commit**

  ```bash
  git add tests/test_builder.py tests/test_execution_intent.py tests/test_config.py tests/test_strategy.py tests/test_engine.py tests/test_dashboard.py
  git commit -m "test: cover delta-neutral builder integration"
  ```

## Edge-case decisions fixed by the approved spec

1. **Quantity/notional drift:** session progress uses the conservative common matched gross notional from actual entry fills: `min(actual Entropy qty, actual hedge qty) * min(actual Entropy average price, actual hedge average price)`. The fixed-size intent uses the maximum executable entry price when converting a USD request to quantity, so no leg intentionally exceeds the requested chunk before venue precision.
2. **Final chunk below minimum:** a positive remainder below the effective minimum is not overshot and is not silently marked complete. The builder stays in a non-trading `WAIT BASIS`/hold state with `TARGET_REMAINDER_BELOW_MINIMUM`; the session target remains visible.
3. **Partial fills plus hedge:** the existing lifecycle settles residual inventory first. The Engine sends one finalized result after the hedge/reconcile outcome is authoritative; only the common entry match is credited, never the requested or submitted quantity.
4. **Existing imbalance:** startup baseline is display/accounting context only. No single-leg correction is generated from the difference between venue baselines.
5. **Position caps below target:** common Engine cap/risk gates and `BuilderCapacity` produce `CAPACITY_BLOCKED`; no order bypasses a cap and remaining target is retained.
6. **Shutdown during execution:** existing `_exec_tasks` and settlement timeout behavior remain authoritative. In-flight executions are allowed to settle under current Engine shutdown semantics; builder session progress is not persisted or resumed.

## Spec-to-task coverage checklist

- Purpose/non-goals and explicit human selection: Tasks 1, 9, and 11.
- Existing architecture and narrow intent boundary: Tasks 3, 5, and 9.
- Configuration and strict validation: Task 1.
- Fresh startup baseline and no imbalance correction: Tasks 4 and 9.
- Actual finalized matched-progress accounting: Tasks 4 and 5.
- Executable formulas and one threshold per direction: Task 2.
- Common safety prerequisites and no inventory surcharge: Tasks 7–9.
- Fixed chunk, min/max/cap/risk clamp, no overshoot: Tasks 3 and 7.
- Retry/cooldown/in-flight/completion state machine: Task 6.
- Existing execution, hedge, reconcile, slippage, fee, and telemetry reuse: Task 5.
- Completion/HOLD and no unwind: Tasks 6, 7, and 11.
- Existing rolling center source and causal semantics: Tasks 1 and 9.
- Dashboard/status read-only behavior: Task 10.
- Error/restart/fail-closed behavior: Tasks 4, 6, 9, and 11.
- All 33 numbered test requirements: Task 11's explicit matrix, with pure unit coverage in Tasks 1–8 and integration coverage in Tasks 9–11.
- Stable-basis invariants: every Engine task includes the stable regression command; Task 11 retains dedicated legacy-path assertions for threshold arithmetic, direction, `plan_arb()`, inventory, rate limits, stale behavior, hedge/reconcile, `fill_edge`, `total_fill_edge`, execution telemetry, and CSV semantics.

## Self-review gate before implementation begins

- [ ] Compare sections 1–18 of the approved spec against the checklist above and record a concrete test/task for each requirement.
- [ ] Review every checkbox for concrete commands, assertions, and interfaces; no vague implementation step may remain.
- [ ] Verify every type and function name in a later task matches the producing task (`BuilderConf`, `BuilderDirection`, `BuilderBBO`, `BuilderDecision`, `FixedSizeExecutionIntent`, `BuilderExecutionResult`, `BuilderSession`, `BuilderCapacity`, `BuilderStatusSnapshot`, `BuilderSettlementSink`).
- [ ] Confirm the only new production files are `entropy_arb/builder.py` and `entropy_arb/execution_intent.py`, and that all other file changes are limited to the listed existing modules/tests.
- [ ] Confirm the implementation tasks contain no funding, market-diagnosis, regime, adaptive-threshold, lead/lag, persistence, automatic-selection, unwind, new venue, or websocket-quota work.
- [ ] Confirm stable-basis compatibility has explicit RED/GREEN gates in Tasks 1, 5, 8, 9, 10, and 11.
- [ ] Run `git diff --check` before committing the plan and again after every implementation task.
