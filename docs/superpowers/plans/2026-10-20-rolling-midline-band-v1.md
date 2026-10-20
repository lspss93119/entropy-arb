# Rolling Midline and Band v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add and validate an isolated 8-hour/60-minute Rolling parameter
calculator while preserving the existing executable-room research method and
all production trading behavior.

**Base:** `origin/main` at
`8857f31b8e275ef8911291acaac39d2c3c15dfa1`.

**Branch:** `feature/rolling-midline-band-v1`.

**Spec:** `docs/superpowers/specs/2026-10-20-rolling-midline-band-v1-design.md`.

## Constraints

- Preserve existing untracked `research/` and `docs/superpowers/` files.
- Do not modify `engine.py`, `config.py`, `recorder.py`, Execution Journal,
  order planning, inventory rules, or formal strategy settings.
- Do not access credentials, start live processes, or send orders.
- Use only isolated historical replay and deterministic unit tests.
- Do not bridge data segments or treat ending MTM as realized PnL.

## Task 1: Baseline and contract tests (RED first)

**Files:**

- Add `tests/test_rolling_midline_band.py`.
- Read-only review of `entropy_arb/engine.py`, `tools/analyze.py`, and
  `entropy_arb/recorder.py`.

**Steps:**

1. Run the named baseline checks and record the known Dashboard failure
   separately.
2. Add failing tests for the public sample/parameter contract, exact UTC
   boundary exclusion, linear-interpolated P90, strict quality failures,
   duplicate timestamps, non-finite Premium, cross-market mixing, and no
   future-data use.
3. Add failing tests for the 60-minute aligned iterator and invalid output
   diagnostics.
4. Run only the new test file and confirm failures are due to the missing
   module, not test collection or environment errors.

## Task 2: Pure rolling calculator (GREEN)

**Files:**

- Add `entropy_arb/rolling.py`.
- Modify only `tests/test_rolling_midline_band.py` as needed to complete the
  contract.

**Steps:**

1. Implement immutable typed sample and parameter records using only the
   standard library.
2. Implement a single-window calculation with `[effective_at-8h,
   effective_at)` semantics and the predeclared quality gate.
3. Implement the Premium-deviation P90 comparison method exactly as specified;
   do not implement it as an Engine setting.
4. Implement the UTC-aligned 60-minute iterator without looking ahead.
5. Run focused tests until green; keep the module independent of venue/API,
   config, and Engine imports.

## Task 3: Isolated historical replay comparison

**Files:**

- Add an isolated research script under `/private/tmp/` during execution, or
  add a narrowly scoped tool only if needed for reproducibility; do not alter
  production strategy code.
- Read `research/data/long-history-export/*.csv` and the existing frozen
  research artifacts under `/private/tmp/entropy-arb-11market-research.4drw89/`.

**Steps:**

1. Load only the seven canonical long-history exports with explicit symbol and
   hedge validation.
2. Split by recorded contiguous segment; never fill or bridge missing minutes.
3. Generate 8-hour/60-minute parameter rows and write an isolated report with
   valid/invalid reasons and method formulas.
4. Reproduce the legacy executable-room P90 method exactly, including its
   one-decimal midline convention and fee argument, for comparison only.
5. Replay the existing fixed-band/inventory-ladder model with observed BBO
   prices for both methods and declared costs 0, 0.5, 1, and 2 bps per leg.
6. Report SELL/BUY signals, completed round trips, maximum/ending inventory,
   ending MTM, and wait/segment stability. Label observed-BBO model outputs as
   theoretical/replay results, not realized PnL.
7. Confirm that every rolling parameter uses only rows strictly before its
   `effective_at` and that no valid row crosses a gap.

## Task 4: Historical replay tests

**Files:**

- Extend `tests/test_rolling_midline_band.py` or add a focused replay test
  module if the existing test structure requires it.

**Steps:**

1. Test a synthetic segment with an exact known median and tail P90.
2. Test a missing minute and confirm no valid parameter or replay signal is
   produced for that window.
3. Test a boundary row at `effective_at` and confirm it is excluded.
4. Test fixed-method comparison data independently so the two methods cannot
   be silently conflated.

## Task 5: Full verification and delivery

**Steps:**

1. Run `python -m pytest tests/ -q`.
2. Run `python -m compileall -q entropy_arb tests`.
3. Run `git diff --check`.
4. Review the diff for Engine/config/Journal changes, credentials, and
   accidental modification of pre-existing untracked files.
5. Commit only the Rolling module, its tests, and the approved design/plan
   documents on `feature/rolling-midline-band-v1`.
6. Push the feature branch and re-read its final SHA.
7. Report starting/ending SHA, formulas, quality gate, data coverage, replay
   metrics, known Dashboard baseline failure separation, and whether Engine
   integration is `PASS` or `BLOCK`.
