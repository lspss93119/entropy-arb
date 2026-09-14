# Dynamic Threshold Shadow Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改變現有固定門檻與下單行為的前提下，加入每交易對獨立的動態門檻 Shadow calculator，使用最近 12 小時的有效 minute rows，每 15 分鐘產生一次 snapshot。

**Architecture:** `MinuteRecorder` 在完成一列 minute row 後透過可選 callback 傳遞資料；新的 `DynamicThresholdController` 只在記憶體保存必要欄位，執行品質檢查與 median/P90 計算；`Engine` 只負責生命週期與接線。現有策略仍只讀取 `Config` 的固定 `midline_bps`、`upper_bps`、`lower_bps`。

**Tech Stack:** Python 3、現有 `csv`／`dataclasses`／`collections.deque`／`datetime`，以及既有 pytest 測試套件；不新增第三方依賴、不連接交易所、不使用憑證。

**Spec:** [`docs/superpowers/specs/2026-09-14-dynamic-thresholds-shadow-design.md`](/Users/liaoyuchen/entropy-arb/docs/superpowers/specs/2026-09-14-dynamic-thresholds-shadow-design.md)

## Global Constraints

- 第一階段只支援 `mode: off` 與 `mode: shadow`；不實作 live/canary 模式。
- `thresholds:` 仍是實盤策略唯一的門檻來源；Shadow snapshot 不得寫回 `Config`。
- 不改變交易所 adapter、下單、滑點、cooldown、倉位、reconciliation 或既有 recorder CSV 欄位。
- Calculator 只保存每交易對最近 12 小時的五個必要欄位：`minute_ts`、`samples`、`premium_close_bps`、`sell_edge_max_bps`、`buy_edge_max_bps`。
- 僅使用 `samples >= 10` 的 minute row；至少 80% 覆蓋率（預設 12 小時窗口即至少 576 筆）才可產生 `valid` snapshot。
- `warming_up` 時動態數值欄位留空；資料品質恢復前 `frozen` 保留最後有效值但不更新。
- Snapshot 寫入 repo 內的 `logs/engine/dynamic-thresholds-<symbol>-<hedge>.csv`，不保存第二份完整 minute dataset。
- `config.yaml`、`.env`、EC2 runtime 檔案與未追蹤檔案不加入 source commit；部署與啟用另行授權。
- 測試必須是離線、可重複、無真實訂單、無外部 API 呼叫。

---

## Task 1: Add typed configuration with fail-closed validation

**Files:**

- Modify `/Users/liaoyuchen/entropy-arb/entropy_arb/config.py`
- Modify `/Users/liaoyuchen/entropy-arb/config.example.yaml`
- Modify `/Users/liaoyuchen/entropy-arb/tests/test_config.py`

**Steps:**

- [ ] Add a frozen `DynamicThresholdConfig` dataclass beside `Config` with `mode`, `window_hours`, `update_minutes`, `percentile`, `floor_bps`, `min_coverage_pct`, and `seed_from_csv`.
- [ ] Add `dynamic_thresholds` to `Config` and `_SCHEMA` with strict scalar types so unknown keys remain startup errors.
- [ ] Parse missing `dynamic_thresholds` as `mode="off"` to preserve existing configurations; parse the example file with the explicitly documented Shadow values.
- [ ] Validate `mode` as `off|shadow`, `window_hours > 0`, `update_minutes > 0`, `0 < percentile < 100`, `floor_bps >= 0`, and `0 < min_coverage_pct <= 100`; calculate the minimum row count from the configured window and coverage rather than hard-coding 576.
- [ ] Reject Shadow mode when `recorder.enabled` is false, with a clear `ConfigError`; do not silently create another recorder or feed.
- [ ] Add the documented `dynamic_thresholds:` block to `config.example.yaml` without changing the existing fixed `thresholds:` block.
- [ ] Add RED tests for defaults, valid Shadow settings, each invalid value, unknown nested keys, and the recorder-disabled safety error.

**Verification:**

```bash
python3 -m pytest tests/test_config.py
```

## Task 2: Implement the pure rolling calculator and snapshot writer

**Files:**

- Add `/Users/liaoyuchen/entropy-arb/entropy_arb/dynamic_thresholds.py`
- Add `/Users/liaoyuchen/entropy-arb/tests/test_dynamic_thresholds.py`

**Steps:**

- [ ] Define a compact immutable minute sample type containing only the five required fields and a snapshot type containing status, quality fields, thresholds, changes, and reason.
- [ ] Implement `DynamicThresholdController` with explicit constructor inputs for symbol, hedge, dynamic config, recorder CSV path, output CSV path, combined fee bps, and an injectable clock for deterministic tests.
- [ ] Load the last 12 hours from the existing pair-specific recorder CSV when `seed_from_csv` is true; parse only valid rows and ignore malformed rows with a recorded reason.
- [ ] Maintain a bounded `deque` and evict entries older than the configured window; never retain the full recorder row or write a duplicate minute dataset.
- [ ] Add an ingestion method that accepts a completed recorder row, filters `samples < 10`, updates the rolling buffer, and only invokes calculation at one UTC update bucket per `update_minutes` interval.
- [ ] Implement the exact formulas from the spec: median of `premium_close_bps`; sell room `sell_edge_max_bps - midline - fees_bps`; buy room `buy_edge_max_bps + midline - fees_bps`; raw P90 values; effective values floored by `floor_bps`.
- [ ] Implement `warming_up`, `valid`, and `frozen` transitions. A bad window must retain the last valid values, stop updating while invalid, and resume only after a later quality check passes.
- [ ] Keep warmup threshold fields empty; include `valid_minutes`, coverage percentage, gap count, window bounds, previous-value deltas, and a human-readable reason in each snapshot.
- [ ] Implement a header-safe append writer for `logs/engine/dynamic-thresholds-<symbol>-<hedge>.csv`, with at most one row per update bucket and no historical snapshot rewrite after restart.
- [ ] Add RED/GREEN tests for percentile math, floor behavior, rolling eviction, 12-hour seed loading, the configured 80% warmup boundary (576 rows for the default), malformed rows, missing intervals, freeze/resume, update bucket deduplication, and snapshot schema.

**Verification:**

```bash
python3 -m pytest tests/test_dynamic_thresholds.py
```

## Task 3: Add a non-invasive recorder completion callback

**Files:**

- Modify `/Users/liaoyuchen/entropy-arb/entropy_arb/recorder.py`
- Modify `/Users/liaoyuchen/entropy-arb/tests/test_recorder.py`

**Steps:**

- [ ] Add an optional `on_minute` callback parameter to `MinuteRecorder.__init__`, preserving all existing call sites and defaults.
- [ ] In `_flush_agg`, build the existing row once, write and flush it exactly as today, then call the callback with that completed row.
- [ ] Keep the callback synchronous and lightweight; it must only deliver the completed row to the in-process calculator and must not open a feed or change the recorder CSV.
- [ ] Catch and log callback exceptions so a Shadow failure cannot stop minute recording or alter live execution.
- [ ] Add tests proving the callback receives rows only after successful minute completion, existing CSV output is byte/schema compatible, partial close behavior remains unchanged, and callback errors do not prevent recorder shutdown.

**Verification:**

```bash
python3 -m pytest tests/test_recorder.py
```

## Task 4: Wire Shadow mode into Engine without changing strategy inputs

**Files:**

- Modify `/Users/liaoyuchen/entropy-arb/entropy_arb/engine.py`
- Modify `/Users/liaoyuchen/entropy-arb/tests/test_engine.py`

**Steps:**

- [ ] Add an optional controller field to `Engine` and create it only when `cfg.dynamic_thresholds.mode == "shadow"`.
- [ ] Build the pair-specific snapshot path using the existing safe symbol/hedge naming convention under `logs/engine/`, without modifying `main.py` output names or the existing recorder/trades paths.
- [ ] Seed the controller from the already-namespaced `cfg.recorder_csv` before the recorder task starts; pass its ingestion callback into the same `MinuteRecorder` instance.
- [ ] Pass `self.entropy.fee_bps + self.hedge.fee_bps` as the fixed fee input after venue metadata is loaded; do not estimate or mutate fees.
- [ ] Close/flush the controller during the existing Engine shutdown path, including record-only and live shutdowns.
- [ ] Ensure the strategy loop, `_eff_threshold`, `_plan`, status display, run logging, and order execution continue to read only `cfg.midline_bps`, `cfg.upper_bps`, and `cfg.lower_bps`.
- [ ] Add tests proving `mode: off` creates no controller, Shadow mode uses the same recorder instance, fixed thresholds remain unchanged, and Shadow exceptions do not alter strategy evaluation.

**Verification:**

```bash
python3 -m pytest tests/test_engine.py tests/test_run_logging.py
```

## Task 5: Add offline replay and full regression verification

**Files:**

- Modify `/Users/liaoyuchen/entropy-arb/tests/test_dynamic_thresholds.py` only if replay coverage needs a shared fixture.
- Do not modify `/Users/liaoyuchen/entropy-arb/tools/analyze.py` or `/Users/liaoyuchen/entropy-arb/tools/analyze_trades.py`.

**Steps:**

- [ ] Replay `/Users/liaoyuchen/entropy-arb/logs/aws-download/record/minutes-SNDK-lighter-rh.csv` through the controller with a fixed clock and verify 12-hour/15-minute snapshots are deterministic.
- [ ] Replay at least one pair with known gaps and one pair with regime movement to verify `frozen`/resume and changing threshold snapshots.
- [ ] Confirm output contains no credentials, no order fields, no full raw minute rows, and no duplicate update bucket.
- [ ] Run the complete offline test suite and compile check.

**Verification:**

```bash
python3 -m pytest
python3 -m compileall -q main.py entropy_arb tools
git diff --check
```

## Task 6: Controlled observation handoff (separate authorization)

**No source files are changed by this task.**

**Steps:**

- [ ] Review the diff and tests locally.
- [ ] Add the `dynamic_thresholds:` block to the operator-owned local `config.yaml` only after explicit runtime authorization; leave `thresholds:` unchanged.
- [ ] Run local Shadow mode and inspect `/Users/liaoyuchen/entropy-arb/logs/engine/dynamic-thresholds-*.csv`.
- [ ] Only after local verification, explicitly authorize upload to EC2 and observe for at least 24 hours.
- [ ] Do not enable dynamic thresholds for order decisions; canary design remains a separate future task.

**Verification:**

```bash
find /Users/liaoyuchen/entropy-arb/logs/engine -name 'dynamic-thresholds-*.csv' -print
```

## Completion checklist

- [ ] All tests pass without credentials or external API calls.
- [ ] `mode: off` is behaviorally identical to the pre-feature engine.
- [ ] Shadow output is per pair, bounded, timestamped, and quality-labelled.
- [ ] Existing recorder minute CSVs are unchanged.
- [ ] Existing fixed thresholds and live order behavior are unchanged.
- [ ] No `.env`, private keys, ignored runtime config, or untracked artifacts are committed.
