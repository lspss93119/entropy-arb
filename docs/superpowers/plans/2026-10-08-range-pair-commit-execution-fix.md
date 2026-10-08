# Range Pair-Commit Execution Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a Range pair from submitting one primary leg and then vetoing the other on a fresh-plan guard by moving authoritative validation and Lighter nonce preparation before a single pair commit.

**Architecture:** Keep the existing Range reservation and verdict semantics, but make `_execute()` perform one final pair-level verdict after an explicit Lighter nonce reservation and before either venue transport. A committed pair sends both primaries without another strategy-plan guard; pre-commit aborts release the prepared nonce and clear the unsent intent. Preserve the existing venue APIs for non-Range execution and retain diagnostics in execution CSV rows.

**Tech Stack:** Python 3, asyncio, pytest, existing Entropy/Hyperliquid/Lighter venue adapters.

**Spec:** User-provided attached pair-commit execution-fix request based on events `1791444610170-000008` and `1791444610170-000012`.

## Global Constraints

- Do not deploy or restart production; the current production inventory is non-flat.
- Do not change strategy parameters, cap `$1,500`, max adjustment `$53/min`, depth `<=75%`, signal age `<=15s`, range gate `10bps`, fallback semantics, or HALT/reconciliation semantics.
- Keep Lighter SDK nonce manager and the application nonce lock; do not retry, locally convert, or reuse an aborted nonce.
- Before pair commit, either both primary transports are unattempted or the pair is not submitted; after commit, both primary send paths are attempted and no fresh-plan per-leg veto occurs.
- Do not clean unrelated Ruff baseline findings.

## Review Focus

- Pre-commit fresh-plan shrink below `min_base` must abort with zero transports; test the `000012` shape.
- Favorable reserved-price movement must not veto the pair; test the `000008` sell-bound shape and both-side reserved-bound semantics.
- A prepared nonce must be held through abort or RH transport and never reused after abort; cover preparation failure and next-attempt nonce identity.
- HALT, stop, rate budget, adverse reserved-price movement, and current planned quantity shrink must remain pair-level zero/zero outcomes.
- Post-commit transport errors must retain existing fallback/reconciliation behavior without introducing a one-leg guard veto.

---

### Task 1: Pair-level pre-commit state machine and diagnostics

**Files:**
- Modify: `entropy_arb/engine.py` (`_prepare_range_submit`, `_range_submission_verdict`, `_execute`, CSV header/row writer)
- Test: `tests/test_range_inventory_engine.py`

**Interfaces:**
- Consumes: the existing reserved `RangePlan`, `_range_submission_verdict(...)`, `RangeInventoryLive.reserve/abandon_unsent`, and venue `send_taker`.
- Produces: a Range execution path that returns an unsent pre-commit result when pair validation fails, and a committed submission context containing reserved bounds, pair verdict diagnostics, `pair_committed`, and optional prepared nonce.

- [x] **Step 1: Write failing tests** for one pair commit where both transports are attempted, for `000012` fresh-plan `below_min_base` abort with zero transports, for final fresh-qty shrink zero/zero, adverse buy/sell reserved-bound failures zero/zero, rate/HALT failures zero/zero, and the two invariants (`pair_committed=false` never exactly one transport; `pair_committed=true` never has a fresh-plan per-leg veto).
- [x] **Step 2: Run the focused Range tests** and verify they fail because the current engine invokes independent per-leg guards and can transport one leg before the other vetoes.
- [x] **Step 3: Implement the minimal pair state machine** in `engine.py`: reserve intent first, run exactly one final pair-level `_range_submission_verdict` before transport, clear intent on abort, mark the pair committed only after that verdict passes, and call both primary venues without passing a fresh-plan guard after commit.
- [x] **Step 4: Append pair-commit fields** (`nonce_prepared_ts`, `nonce_prepare_ms`, `final_pair_preflight_ts`, `final_pair_preflight_reason`, `pair_committed_ts`, `pair_committed`, `entropy_transport_attempted`, `rh_transport_attempted`) while preserving existing columns and old CSV compatibility.
- [x] **Step 5: Run the focused Range suite** and confirm all new and retained state/fallback tests pass.

### Task 2: Lighter nonce preparation and ownership

**Files:**
- Modify: `entropy_arb/venue_lighter.py`
- Test: `tests/test_lighter_nonce.py`

**Interfaces:**
- Consumes: `LighterVenue._nonce_lock`, SDK `nonce_manager.async_next_nonce`, existing `send_taker` diagnostics.
- Produces: `await prepare_nonce()`, `release_prepared_nonce(prepared)`, and `send_taker(..., prepared_nonce=prepared)`; the prepared token records authoritative nonce timing and owns the lock until abort or RH transport submission returns.

- [x] **Step 1: Write failing tests** for nonce preparation timing, pre-commit abort releasing without reuse, preparation failure causing no submission, and committed send consuming the prepared nonce while preserving existing nonce/error behavior.
- [x] **Step 2: Run the focused nonce tests** and verify the new API/tests fail before implementation.
- [x] **Step 3: Implement the nonce reservation token** with one SDK nonce fetch under the existing lock, explicit consumed/aborted ownership, release on pair abort, and no retry or reuse; keep legacy `send_taker` callers on the existing lock/fetch path.
- [x] **Step 4: Run the nonce tests plus the Range pair tests** and confirm prepared timing and transport diagnostics are populated without changing non-Range behavior.

### Task 3: Regression coverage and full verification

**Files:**
- Modify: `tests/test_range_inventory_engine.py`, `tests/test_lighter_nonce.py`, and any narrowly required test fixtures only.

- [x] **Step 1: Add/adjust tests** for persisted `000008` favorable sell, persisted `000012` fresh-plan shrink, normal commit diagnostics, pre-commit nonce failure, post-commit transport failure with existing fallback, and unchanged Range state/reconciliation behavior.
- [x] **Step 2: Run `python3 -m pytest -q`** and record the complete result.
- [x] **Step 3: Run `git diff --check` and a semantic diff review** confirming the patch only changes pair submission ordering/nonce preparation/diagnostics, not strategy, limits, fallback rules, or HALT behavior.
- [ ] **Step 4: Commit and push the new `codex/range-pair-commit` branch**; do not deploy it.
