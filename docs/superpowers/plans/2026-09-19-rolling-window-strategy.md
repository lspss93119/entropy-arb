# Rolling Window Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit `fixed`/`rolling` strategy switch with a walk-forward rolling-window live signal while preserving fixed-threshold behavior by default.

**Architecture:** A pure `RollingWindow` calculator consumes completed recorder rows and exposes immutable snapshots/signals. The engine selects fixed or rolling signal construction, while both modes share existing BBO planning, venue locks, concurrent execution, residual hedging, and reconciliation. Rolling exits use a force-close `ArbPlan` marked `reduce_only`.

**Tech Stack:** Python 3, dataclasses, csv, PyYAML, asyncio, pytest.

**Spec:** `docs/superpowers/specs/2026-09-19-rolling-window-strategy-design.md`

## Global Constraints

- `strategy.mode: fixed` remains the default and keeps the current fixed thresholds path unchanged.
- Rolling snapshots use only completed rows before the current update block; no look-ahead is allowed.
- Invalid rolling snapshots fail closed for new entries; never fall back silently to fixed thresholds.
- Rolling startup rejects non-flat reconciled positions because inherited direction cannot be inferred safely.
- All primary and hedge order outcomes continue through existing reconciliation and unresolved-outcome handling.
- No new runtime dependency and no recorder CSV schema change.

## Review Focus

- A current-block recorder row must not influence the snapshot used by that block; owned by Task 1 walk-forward tests.
- An invalid/zero-dispersion snapshot must block entries rather than reuse fixed thresholds; owned by Task 1 and Task 4 tests.
- A timeout exit must still be reduce-only and must not be blocked by the normal executable-edge hurdle; owned by Task 2 and Task 4 tests.
- A partial entry/exit must not create a false flat state; owned by Task 4 execution-state tests.
- A rolling process started with inherited positions must fail closed before strategy tasks start; owned by Task 4 startup test.

---

### Task 1: Rolling configuration and pure calculator

**Files:**
- Create: `entropy_arb/rolling.py`
- Modify: `entropy_arb/config.py`
- Modify: `tests/test_config.py`
- Create: `tests/test_rolling.py`

**Interfaces:**
- Consumes: `RollingConf` from `entropy_arb.config` with the fields in the spec.
- Produces: `RollingSnapshot`, `RollingSignal`, and `RollingWindow(config)` with `ingest_row`, `load_csv`, `snapshot_for`, `entry_signal`, and `exit_signal`.

- [ ] **Step 1: Write the failing tests**

Add tests for rolling config defaults/validation and these calculator behaviors:

```python
def test_snapshot_excludes_current_update_block():
    window = RollingWindow(RollingConf(window_hours=1, update_minutes=15,
                                       min_coverage_pct=50))
    for minute in range(60):
        window.ingest_row({"minute_ts": minute * 60,
                           "samples": "1",
                           "premium_close_bps": "0"})
    window.ingest_row({"minute_ts": 60 * 60,
                       "samples": "1", "premium_close_bps": "1000"})
    snapshot = window.snapshot_for(60 * 60)
    assert snapshot.valid and snapshot.mean_bps == 0.0

def test_invalid_snapshot_blocks_entry_and_timeout_exits():
    config = RollingConf(window_hours=1, update_minutes=15,
                         min_coverage_pct=100, timeout_hours=1)
    window = RollingWindow(config)
    assert window.entry_signal(10.0, now=3600.0,
                               entropy_spread_bps=1.0,
                               hedge_spread_bps=1.0) is None
    signal = window.exit_signal(10.0, now=3600.0,
                                entropy_spread_bps=50.0,
                                hedge_spread_bps=50.0,
                                entry_ts=0.0,
                                direction="sell_entropy")
    assert signal is not None and signal.reason == "timeout"
```

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `python3 -m pytest -q tests/test_rolling.py tests/test_config.py`

Expected: FAIL because `RollingConf` and `RollingWindow` do not yet exist and `rolling` config keys are rejected.

- [ ] **Step 3: Implement the minimal configuration and calculator**

Add `RollingConf`, `strategy_mode`, and the `rolling` schema/load/validation to `config.py`. Implement `RollingWindow` with a timestamp-deduplicated point store, CSV seeding, strict pre-block snapshots, finite positive-dispersion validation, z-score entry gates, spread-gated exits, and timeout exits. Keep all calculations synchronous and pure apart from the in-memory store.

- [ ] **Step 4: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_rolling.py tests/test_config.py`

Expected: all focused tests pass.

Run: `python3 -m pytest -q`

Expected: the existing suite and the new calculator/config tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/config.py entropy_arb/rolling.py tests/test_config.py tests/test_rolling.py
git commit -m "feat: add rolling window configuration and calculator"
```

### Task 2: Executable force-close plan

**Files:**
- Modify: `entropy_arb/book.py`
- Modify: `tests/test_book.py`

**Interfaces:**
- Consumes: existing `plan_arb` book walking and `ArbPlan`.
- Produces: `ArbPlan.reduce_only` (default `False`) and `plan_arb(..., require_edge=False)` for rolling exits.

- [ ] **Step 1: Write the failing tests**

Add a test with a crossed-in-the-wrong-direction BBO showing that normal planning returns `no_edge`, while `require_edge=False` returns a plan with a negative expected edge and the default `reduce_only` value remains false. Add a second assertion that `dataclasses.replace(plan, reduce_only=True)` is accepted by the execution path’s plan type.

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `python3 -m pytest -q tests/test_book.py -k force_close`

Expected: FAIL because `plan_arb` has no `require_edge` argument and `ArbPlan` has no `reduce_only` field.

- [ ] **Step 3: Implement the minimal force-close support**

Add `require_edge=True` to the existing book walk; when false, walk available levels without rejecting negative instantaneous edge, while still enforcing positive quantities, size step, minimum base, and minimum notional. Add `reduce_only: bool = False` to `ArbPlan` without changing existing constructor call sites.

- [ ] **Step 4: Run focused and full book tests**

Run: `python3 -m pytest -q tests/test_book.py`

Expected: all book tests pass.

Run: `python3 -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/book.py tests/test_book.py
git commit -m "feat: support reduce-only force-close plans"
```

### Task 3: Feed completed recorder rows into rolling state

**Files:**
- Modify: `entropy_arb/recorder.py`
- Modify: `tests/test_recorder.py`

**Interfaces:**
- Consumes: existing `_MinuteAgg.row()` output.
- Produces: optional `on_minute: Callable[[dict], None]` callback invoked after each flushed completed row, including a final partial-minute flush.

- [ ] **Step 1: Write the failing test**

Add a recorder test that passes an `on_minute` callback, crosses a minute boundary, closes the recorder, and asserts callbacks receive the same `minute_ts`/`premium_close_bps` rows that were written to CSV, in order.

- [ ] **Step 2: Run the focused test to verify it fails**

Run: `python3 -m pytest -q tests/test_recorder.py -k callback`

Expected: FAIL because `MinuteRecorder` does not accept or invoke `on_minute`.

- [ ] **Step 3: Implement the callback**

Add the optional callback parameter, convert the row list to a `{HEADER[i]: row[i]}` mapping after the CSV write/flush, invoke the callback synchronously, and log callback failures without breaking recorder shutdown or CSV persistence.

- [ ] **Step 4: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_recorder.py`

Expected: all recorder tests pass.

Run: `python3 -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add entropy_arb/recorder.py tests/test_recorder.py
git commit -m "feat: expose completed recorder minutes to strategy consumers"
```

### Task 4: Engine fixed/rolling strategy integration

**Files:**
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_engine.py`
- Modify: `tests/test_trade_logging.py`
- Modify: `tests/test_run_logging.py`

**Interfaces:**
- Consumes: `RollingWindow`, `ArbPlan.reduce_only`, and `MinuteRecorder.on_minute`.
- Produces: `strategy.mode` dispatch, one-position rolling state, reduce-only rolling exits, rolling run/trade telemetry, and fixed-mode compatibility.

- [ ] **Step 1: Write failing engine tests**

Add tests for: rolling entry from a valid snapshot; no second entry while open; a z-score exit creates a reverse plan marked `reduce_only`; timeout exit bypasses the normal edge hurdle; startup with a non-flat position raises a clear rolling error; fixed mode continues to fire the existing fixed band; and run config/trade CSVs include rolling fields.

- [ ] **Step 2: Run the focused tests to verify they fail**

Run: `python3 -m pytest -q tests/test_engine.py tests/test_trade_logging.py tests/test_run_logging.py -k rolling`

Expected: FAIL because the engine has no rolling dispatch, state, or telemetry fields.

- [ ] **Step 3: Implement the strategy dispatch and lifecycle**

Instantiate/seed `RollingWindow` only for rolling mode, attach the recorder callback, and split `_scan` into fixed and rolling paths. Keep fixed `_eff_threshold` and plan behavior intact. For rolling mode, use current mid premium and BBO spread gates, existing persistence/cap/rate/freshness checks, threshold `0` for entries, and `require_edge=False` plus `reduce_only=True` for exits. Track one rolling direction and matched quantity; update it only from settled execution results, preserving partial state.

- [ ] **Step 4: Implement reduce-only sends and telemetry**

Pass `plan.reduce_only` into both primary `send_taker` calls. Add rolling mode/snapshot/signal fields to run and trade CSV headers and rows. Keep run config writes disabled for record-only processes. Add a rolling status summary without changing fixed status semantics.

- [ ] **Step 5: Run focused and full tests**

Run: `python3 -m pytest -q tests/test_engine.py tests/test_trade_logging.py tests/test_run_logging.py`

Expected: all focused engine/logging tests pass.

Run: `python3 -m pytest -q`

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add entropy_arb/engine.py tests/test_engine.py tests/test_trade_logging.py tests/test_run_logging.py
git commit -m "feat: add selectable rolling window execution strategy"
```

### Task 5: Example configuration and offline smoke verification

**Files:**
- Modify: `config.example.yaml`
- Modify: `README.md`
- Modify: `README.zh-CN.md`

**Interfaces:**
- Consumes: final `Config` fields and CLI behavior.
- Produces: documented fixed default and explicit rolling activation instructions without placing orders.

- [ ] **Step 1: Write the failing documentation/smoke check**

Add a small test that loads `config.example.yaml`, asserts `strategy_mode == "fixed"`, and checks the example contains the rolling keys. Add a generated two-hour recorder CSV smoke command to the test or a pure helper invocation; it must produce a valid rolling snapshot without credentials.

- [ ] **Step 2: Run the check to verify it fails**

Run: `python3 -m pytest -q tests/test_config.py -k example_rolling`

Expected: FAIL because the example has no rolling section and no rolling mode field.

- [ ] **Step 3: Update examples and documentation**

Document that fixed remains default, rolling is activated by `strategy.mode: rolling`, rolling needs a completed window before entries, and record-only is still the safe data-collection mode. Keep credentials and real-order warnings unchanged.

- [ ] **Step 4: Run smoke and full verification**

Run: `python3 -m pytest -q tests/test_config.py -k example_rolling`

Expected: PASS.

Run: `python3 -m compileall -q main.py entropy_arb tools`

Expected: exit 0 with no output.

Run: `python3 -m pytest -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add config.example.yaml README.md README.zh-CN.md tests/test_config.py
git commit -m "docs: document rolling strategy activation and safety gates"
```
