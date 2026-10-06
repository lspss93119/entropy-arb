# Range Inventory Live Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in safe live Range Inventory adapter sharing the exact shadow signal core.

**Architecture:** Pure causal core feeds the unchanged shadow simulator and a thin live adapter. Independent atomic state and plans integrate with existing execution/HALT/reconciliation; no new trading transport.

**Tech Stack:** Python 3.13, dataclasses, stdlib JSON/CSV, existing `plan_arb`, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-06-range-inventory-live-design.md`

## Global Constraints

- Isolated branch from a81eb822; production and EC2 processes/config/state untouched.
- No real orders, deploy, HALT clear, existing mismatch repair, merge or push.
- Exact frozen percentile formulas; live caps 1500/1500/1500, 53 USD/minute, depth <=75%, signal age <=15s.
- Range reductions use neither rolling threshold nor lot BE gate.
- Existing fixed/rolling, nonce, residual hedge, HALT and reconciliation protections retained.
- No secrets in fixtures, telemetry or parity evidence; funding excluded.

## Review Focus

1. Interrupted pre-submit write / restart with intent: no duplicate or unaccounted order (Task 2/3 tests).
2. Delayed callback or shutdown partial minute: cannot execute stale/incomplete signals (Task 2/3 tests).
3. Partial fills plus residual hedge: actual paired quantity, no false flat, unknown prices unavailable (Task 2/3 tests).
4. Opposite direction at zero and depth/minimum boundaries: no same-minute reversal/illegal enlargement (Task 2 tests).
5. Parameter/identity-corrupt state and active HALT: no silent reset or unsafe resume (Task 2/3 tests).

### Task 1: Shared pure strategy core and unchanged shadow

**Files:** create `entropy_arb/range_inventory.py`, `tests/test_range_inventory.py`; modify `entropy_arb/range_inventory_shadow.py`.

**Interfaces:** produce `FrozenRangeInventoryParams`, `RangeSignal`, `RangeInventoryCore(params, use_range_gate)`, `warmup_row(row)`, `on_row(row, inventory_usd)` and `signal_action(inventory_usd, target_usd)`; preserve shadow public imports/CSV.

- [ ] Write tests for core metadata, causal windows, gate add-only, directional target, shared-core ownership and original shadow oracle parity.
- [ ] Run `python3 -m pytest tests/test_range_inventory.py -q`; expected RED for missing core.
- [ ] Extract only signal/target math and validation; shadow keeps all execution/accounting.
- [ ] Run `python3 -m pytest tests/ -q`; expected all PASS, original behavior unchanged.
- [ ] Commit `refactor: share pure range inventory strategy core`.

### Task 2: Atomic independent state and live planning adapter

**Files:** create `entropy_arb/range_inventory_live.py`, `tests/test_range_inventory_live.py`.

**Interfaces:** consume Task 1 core; produce `RangeInventoryLive` with `load_and_reconcile`, `on_minute`, `plan`, `reserve`, `settle`, `expected_positions`; use existing `ArbPlan` and `plan_arb`.

- [ ] Write failing tests for fresh completed-minute immediate plan, stale/partial/duplicate signals, frozen canary profile, gate add-only, 53 budget, 75% depth, headroom/minima, reversal, restart reconciliation, corrupt state/HALT/in-flight intent, actual partial/residual fills and price-unavailable telemetry.
- [ ] Run `python3 -m pytest tests/test_range_inventory_live.py -q`; expected RED for missing adapter.
- [ ] Implement strict atomic independent state, single-attempt budget and one-level planner; no API access.
- [ ] Run `python3 -m pytest tests/ -q`; expected all PASS.
- [ ] Commit `feat: add persisted range inventory live adapter`.

### Task 3: Thin opt-in engine integration and safety lifecycle

**Files:** modify `entropy_arb/config.py`, `entropy_arb/engine.py`; create `tests/test_range_inventory_engine.py` and a credential-free example/profile guide.

**Interfaces:** engine routes completed minute and live BBO to Task 2; sends returned plans through existing paired executor and residual hedge; settles actual outcomes and strictly reconciles Range expected positions.

- [ ] Write failing integration tests for opt-in config, completed-minute wakeup, actual no-order stale/HALT guards, partial/residual settlement, strict restart/resume and mismatch HALT, budget persistence before orders and Range reduce without rolling gates.
- [ ] Run `python3 -m pytest tests/test_range_inventory_engine.py -q`; expected RED for mode/integration missing.
- [ ] Integrate only range-mode branches; retain fixed/rolling functions and execution transports unchanged.
- [ ] Run `python3 -m pytest tests/ -q`; expected all PASS.
- [ ] Commit `feat: integrate range inventory with safe paired execution`.

### Task 4: Offline parity evidence and final review

**Files:** create `tools/check_range_inventory_parity.py`, `tests/test_range_inventory_parity.py`; add validation report to docs.

**Interfaces:** consume core, shadow and live adapter signal path; CSV input plus exact old shadow git oracle; fail on any target/action or historical accounting divergence.

- [ ] Write test asserting parity and intentional target/action divergence rejection; run targeted pytest, expected RED for missing runner.
- [ ] Implement offline runner, no env/API; read frozen historical recorder and compare every minute/variant, same profile/inventory and T+1 executor.
- [ ] Run full actual historical parity and canary target/action parity, report input hash/cutoff and exact totals; expected zero divergence.
- [ ] Run full pytest, `python3 -m compileall entropy_arb tools`, `ruff check .`, `git diff --check`; expected PASS.
- [ ] Fresh-context whole-branch review focused on the five risks above; fix important findings with RED/GREEN tests.
- [ ] Read-only verify production SHA/PID/config/env and HALT remains true; no repair.
- [ ] Commit `test: verify range inventory historical and live target parity`; report final clean branch and deployment blockers.
