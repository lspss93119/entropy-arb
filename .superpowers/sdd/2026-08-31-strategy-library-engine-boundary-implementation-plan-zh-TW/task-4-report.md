# Task 4 Report

日期：2026-08-31

起始 HEAD：`dfebf4683fbfd3752432f0bec9774f35b0858d81`

Scope 內修改檔案：

- `entropy_arb/engine.py`
- `tests/test_engine.py`

未修改：

- `strategy.py`
- `config.py`
- `dashboard.py`
- `recorder.py`
- `docs/`
- `tests/test_premium.py`

## RED

先在 `tests/test_engine.py` 補上 Task 4 所需測試，包含：

- `stable_basis` observer helper no-op
- drifting observation 使用 fresh BBO 的 mid premium
- stale / empty / unready 不取樣
- `Engine.premium_bps()` 與 shared `calculate_premiums()` 一致
- live books 持續 observation 後 strategy 進入 `READY`
- observer loop cancellation
- `_run_inner()` 的 stable / drifting / record_only lifecycle wiring

實際執行命令：

```bash
python3 -m pytest /Users/liaoyuchen/entropy-arb/tests/test_engine.py -q
```

實際結果：

```text
........FFF.FF...............
=================================== FAILURES ===================================
AttributeError: 'Engine' object has no attribute '_sample_strategy_observation'
AttributeError: 'Engine' object has no attribute '_strategy_observation_loop'
5 failed, 24 passed, 2 warnings in 0.37s
```

RED 結論：缺少 Task 4 要求的 observation helper 與 observer loop，測試正確失敗。

## GREEN

在 `entropy_arb/engine.py` 完成最小實作：

- 新增 `_sample_strategy_observation(now=None) -> bool`
- 新增 `_strategy_observation_loop()`，約 1 Hz 取樣
- 僅在 `live` 且 `strategy.requires_observations` 時啟動 observer task
- `record_only` 不啟動 strategy / observer
- `Engine.premium_bps()` 改用 shared `calculate_premiums()`，維持既有數值語意
- 保持 `_scan()` fail-closed；strategy 未 ready 時清空兩個 `_armed`
- 不在 Engine 重複實作 `>30s` gap reset，仍由 strategy 自身處理

## 驗證

為避免此環境對 `.pytest_cache` 的寫入權限警告，focused/full suite 使用 cache-free 參數 `-p no:cacheprovider`。

1. 單檔 engine 測試

```bash
python3 -m pytest /Users/liaoyuchen/entropy-arb/tests/test_engine.py -q -p no:cacheprovider
```

```text
................................
32 passed in 0.55s
```

2. Focused suite

```bash
python3 -m pytest /Users/liaoyuchen/entropy-arb/tests/test_strategy.py /Users/liaoyuchen/entropy-arb/tests/test_engine.py -q -p no:cacheprovider
```

```text
........................................
40 passed in 0.71s
```

3. Full suite

```bash
python3 -m pytest -q -p no:cacheprovider
```

```text
........................................................................ [ 47%]
........................................................................ [ 94%]
........                                                                 [100%]
152 passed in 1.06s
```

4. Whitespace / patch hygiene

```bash
git -C /Users/liaoyuchen/entropy-arb diff --check
```

結果：無輸出，通過。

## 變更摘要

- `entropy_arb/engine.py`
  - 接上 shared premium helper
  - 增加 live observation sampling / loop
  - 增加 live drifting strategy task wiring
- `tests/test_engine.py`
  - 增加 Task 4 engine-boundary regression coverage

## Commit

預定 commit message：

```text
feat: feed drifting basis from live premium observations
```
