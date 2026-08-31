# Strategy Library + Engine Boundary 實作計畫

> **給 agentic workers：** REQUIRED SUB-SKILL：使用 `superpowers:subagent-driven-development`（建議）或 `superpowers:executing-plans`，逐 Task 執行本計畫。所有步驟都使用 checkbox (`- [ ]`) 追蹤。

**Goal:** 在不改變既有 execution / hedge / reconcile / risk 行為的前提下，加入 `stable_basis` 與 `drifting_basis` 兩套可由 `config.yaml` 明確選擇的 Strategy Library，並讓未來 Market Analyzer 可重用完全相同的 strategy logic。

**Architecture:** Strategy 只負責「center 在哪裡、是否 ready、upper/lower 是多少」；Engine 繼續負責 freshness、persistence、inventory surcharge、depth walk、sizing、position caps、rate limits、execution、hedge、reconcile。`stable_basis` 使用固定 center；`drifting_basis` 使用約 1 Hz、timestamp-based、causal rolling median。Live 與 historical replay 共用同一個 `strategy.py` 實作。

**Tech Stack:** Python 3、`asyncio`、`dataclasses`、`collections.deque`、`statistics.median`、PyYAML、pytest、Rich dashboard。

**Spec:** `docs/superpowers/specs/2026-08-31-strategy-library-engine-boundary-design-zh-TW.md`。目前已批准的中文版 artifact 為 `2026-08-31-strategy-library-engine-boundary-design-zh-TW.md`；執行前先確保該內容存在於上述 repo 路徑。GitHub connector 在設計階段曾因 403 無法寫入，所以不要假設遠端已經有 spec 檔。

## Global Constraints

- Target branch: `feature/p2-persistence-research`。
- v1 只支援 `stable_basis` 與 `drifting_basis`；不得加入第三套策略。
- 不做自動 market classification、strategy selection 或 strategy switching。
- 不做 Market Analyzer、regime detector、reference-residual strategy、lead-lag filter、locked-PnL exit、pair-lot lifecycle。
- `stable_basis` 在等價參數下必須保持目前 signal / plan / firing 行為。
- `drifting_basis` 只支援 `rolling_median`；不得加入 EWMA、Kalman、HMM、clustering。
- Center observation 固定使用 mid-to-mid `premium_bps`；entry 仍使用 executable BBO。
- Live strategy observation 約 1 Hz；不得以 WebSocket message frequency 加權 center。
- `drifting_basis` 使用 timestamp-based window，不得用 row-count `rolling(3600)` 取代 60 分鐘 window。
- Warm-up coverage 固定 90%，不是 config 參數。
- 超過 30 秒沒有 valid premium observation 時，清空 drifting history 並重新 warm-up；30 秒是固定 safety constant，不是 alpha 參數。
- Process restart 不恢復 drifting state，也不從舊 CSV seed center。
- Strategy 不得依賴 credentials、signer、order API、venue client、account balance、rate limit、hedge 或 reconcile。
- 不新增新的 fee logic；現有 planner / Engine fee 行為保持原樣。
- Exit 行為保持現況：由反向 signal 自然 reduce / flip。
- 每個 Task 以 TDD / characterization tests 驗證，並在 Task 結尾 commit。

---

## File Structure

### 新增

- `entropy_arb/premium.py`
  - 唯一責任：定義 BBO → `premium_bps` / `sell_edge_bps` / `buy_edge_bps` 的純計算。
  - Recorder、Engine strategy observation、未來 historical replay 共用。

- `entropy_arb/strategy.py`
  - 唯一責任：Strategy Library。
  - 定義 `StrategyState`、`StableBasisStrategy`、`DriftingBasisStrategy`、`build_strategy()`。
  - 不 import venue implementation，不呼叫任何 order API。

- `tests/test_strategy.py`
  - Strategy causal rolling-window、warm-up、coverage、gap reset、restart semantics、factory 的主要測試。

- `tests/test_premium.py`
  - Premium 純計算的單元測試與 recorder formula parity。

### 修改

- `entropy_arb/config.py`
  - 用 `StrategyConf` 取代 top-level `midline_bps / upper_bps / lower_bps`。
  - 加入 strategy-specific validation 與 legacy `thresholds:` migration error。

- `entropy_arb/recorder.py`
  - 改用 `entropy_arb.premium.calculate_premiums()`；CSV schema 不變。

- `entropy_arb/engine.py`
  - 建立 strategy object。
  - `_scan()` 從 immutable `StrategyState` snapshot 取得 center/upper/lower。
  - drifting 才啟動約 1 Hz strategy observation loop。
  - unready 時不執行新的 arb，並清除 persistence arming。
  - trades CSV 的 `midline_bps` 欄位保留名稱，但寫入「該次 signal 當下的實際 center」。

- `entropy_arb/dashboard.py`
  - 顯示 strategy name、ready/warming-up、current center、dynamic band。

- `config.example.yaml`
  - 移除 `thresholds:`，新增 `strategy:` 範例。

- `main.py`
  - 更新 CLI 說明文字，不再說「set your thresholds」；明確說 strategy 由 config 人工選擇。

- `README.md`
- `README.zh-CN.md`
  - 更新 config 與兩套 strategy 說明；明確標示沒有自動 strategy selection。

- `tests/test_config.py`
- `tests/test_engine.py`
- `tests/test_recorder.py`
- `tests/test_dashboard.py`
  - 更新既有 fixtures 與新增 regression / observability coverage。

---

### Task 1: 抽出共用 Premium 計算，不改策略行為

**Files:**
- Create: `entropy_arb/premium.py`
- Create: `tests/test_premium.py`
- Modify: `entropy_arb/recorder.py`
- Test: `tests/test_premium.py`
- Test: `tests/test_recorder.py`

**Interfaces:**
- Produces:
  - `PremiumValues`
  - `calculate_premiums(entropy_bid: float, entropy_ask: float, hedge_bid: float, hedge_ask: float) -> PremiumValues`
- Consumers later:
  - `MinuteRecorder`
  - `Engine.premium_bps()`
  - drifting strategy observation loop
  - future Market Analyzer replay

- [ ] **Step 1: 先跑 baseline test suite**

Run:

```bash
python3 -m pytest -q
```

Expected: branch 若仍是目前批准設計時的狀態，應全部 PASS；先前已知 baseline 是 117 tests。若數量不同但全部 PASS，記錄實際數量，不要為了追 117 而修改程式。

- [ ] **Step 2: 寫 `tests/test_premium.py` 的 failing tests**

```python
from entropy_arb.premium import calculate_premiums


def test_calculate_premiums_matches_recorder_definitions():
    v = calculate_premiums(
        entropy_bid=100.10,
        entropy_ask=100.20,
        hedge_bid=99.90,
        hedge_ask=100.00,
    )
    entropy_mid = (100.10 + 100.20) / 2
    hedge_mid = (99.90 + 100.00) / 2
    assert v.premium_bps == pytest.approx((entropy_mid / hedge_mid - 1) * 1e4)
    assert v.sell_edge_bps == pytest.approx((100.10 / 100.00 - 1) * 1e4)
    assert v.buy_edge_bps == pytest.approx((99.90 / 100.20 - 1) * 1e4)


def test_calculate_premiums_is_directionally_consistent():
    v = calculate_premiums(101.0, 101.1, 100.0, 100.1)
    assert v.premium_bps > 0
    assert v.sell_edge_bps > 0
    assert v.buy_edge_bps < 0
```

記得在檔案頂端 `import pytest`。

- [ ] **Step 3: Run tests，確認因 module 不存在而 FAIL**

Run:

```bash
python3 -m pytest tests/test_premium.py -q
```

Expected: FAIL with `ModuleNotFoundError: entropy_arb.premium`。

- [ ] **Step 4: 實作 `entropy_arb/premium.py` 最小版本**

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PremiumValues:
    premium_bps: float
    sell_edge_bps: float
    buy_edge_bps: float


def calculate_premiums(
    entropy_bid: float,
    entropy_ask: float,
    hedge_bid: float,
    hedge_ask: float,
) -> PremiumValues:
    entropy_mid = (entropy_bid + entropy_ask) / 2.0
    hedge_mid = (hedge_bid + hedge_ask) / 2.0
    return PremiumValues(
        premium_bps=(entropy_mid / hedge_mid - 1.0) * 1e4,
        sell_edge_bps=(entropy_bid / hedge_ask - 1.0) * 1e4,
        buy_edge_bps=(hedge_bid / entropy_ask - 1.0) * 1e4,
    )
```

- [ ] **Step 5: Run premium tests，確認 PASS**

Run:

```bash
python3 -m pytest tests/test_premium.py -q
```

Expected: PASS。

- [ ] **Step 6: 將 `recorder.py::_MinuteAgg.add()` 改成共用 helper**

在 `recorder.py`：

```python
from .premium import calculate_premiums
```

將原本三個公式替換為：

```python
values = calculate_premiums(e_bid, e_ask, h_bid, h_ask)
prem = values.premium_bps
sell_edge = values.sell_edge_bps
buy_edge = values.buy_edge_bps
```

其餘 aggregation、CSV schema、flush 行為完全不改。

- [ ] **Step 7: Run recorder + premium tests**

Run:

```bash
python3 -m pytest tests/test_premium.py tests/test_recorder.py -q
```

Expected: PASS，既有 recorder CSV 欄位與數值語義不變。

- [ ] **Step 8: Run full suite**

```bash
python3 -m pytest -q
```

Expected: PASS。

- [ ] **Step 9: Commit**

```bash
git add entropy_arb/premium.py entropy_arb/recorder.py tests/test_premium.py
git commit -m "refactor: share premium calculations"
```

---

### Task 2: 建立 Strategy Library 核心與 drifting causal semantics

**Files:**
- Create: `entropy_arb/strategy.py`
- Create: `tests/test_strategy.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class StrategyState:
    ready: bool
    center_bps: float | None
    upper_bps: float
    lower_bps: float
    window_minutes: int | None = None
    warmup_span_sec: float | None = None
    coverage_ratio: float | None = None
```

兩個 concrete strategy 都必須提供相同 public interface：

```text
update(timestamp: float, premium_bps: float) -> None
state() -> StrategyState
name: str
requires_observations: bool
```

`StableBasisStrategy.name == "stable_basis"` 且 `requires_observations == False`；`DriftingBasisStrategy.name == "drifting_basis"` 且 `requires_observations == True`。

- Fixed constants:

```python
OBSERVATION_INTERVAL_SEC = 1.0
MIN_COVERAGE_RATIO = 0.90
DISCONTINUITY_RESET_SEC = 30.0
```

- [ ] **Step 1: 寫 stable strategy failing tests**

在 `tests/test_strategy.py`：

```python
import math
import pytest

from entropy_arb.strategy import StableBasisStrategy


def test_stable_basis_is_immediately_ready_and_fixed():
    s = StableBasisStrategy(center_bps=-1.0, upper_bps=3.0, lower_bps=3.5)
    before = s.state()
    assert before.ready is True
    assert before.center_bps == -1.0
    assert before.upper_bps == 3.0
    assert before.lower_bps == 3.5

    s.update(1000.0, 25.0)
    after = s.state()
    assert after == before
```

- [ ] **Step 2: 寫 drifting warm-up / causal / gap failing tests**

```python
from entropy_arb.strategy import DriftingBasisStrategy


def feed_seconds(strategy, start, seconds, value_fn=lambda i: float(i)):
    for i in range(seconds):
        strategy.update(start + i, value_fn(i))


def test_drifting_not_ready_before_full_window():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    feed_seconds(s, 1000.0, 60, lambda i: 1.0)
    assert s.state().ready is False
    s.update(1060.0, 1.0)
    assert s.state().ready is True
    assert s.state().center_bps == pytest.approx(1.0)


def test_drifting_requires_90_percent_coverage():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    for i in range(0, 61, 2):
        s.update(1000.0 + i, 2.0)
    state = s.state()
    assert state.warmup_span_sec >= 60
    assert state.coverage_ratio < 0.90
    assert state.ready is False


def test_drifting_uses_timestamp_window_and_causal_median():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    for i in range(61):
        s.update(1000.0 + i, 1.0 if i < 60 else 9.0)
    assert s.state().ready is True
    first_center = s.state().center_bps
    s.update(1061.0, 9.0)
    assert s.state().center_bps >= first_center


def test_short_gap_does_not_reset_history():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    feed_seconds(s, 1000.0, 40, lambda i: 1.0)
    s.update(1060.0, 1.0)  # 21-second gap from last valid observation
    assert s.state().warmup_span_sec >= 60


def test_gap_over_30_seconds_resets_history():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    feed_seconds(s, 1000.0, 61, lambda i: 1.0)
    assert s.state().ready is True
    s.update(1091.1, 5.0)  # >30s since 1060.0
    state = s.state()
    assert state.ready is False
    assert state.warmup_span_sec == pytest.approx(0.0)
    assert state.center_bps is None


def test_nonfinite_premium_is_ignored():
    s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    s.update(1000.0, 1.0)
    before = s.state()
    s.update(1001.0, math.nan)
    s.update(1002.0, math.inf)
    assert s.state() == before
```

- [ ] **Step 3: Run tests，確認 module/classes 尚不存在而 FAIL**

```bash
python3 -m pytest tests/test_strategy.py -q
```

Expected: FAIL。

- [ ] **Step 4: 實作 StrategyState 與 StableBasisStrategy**

在 `entropy_arb/strategy.py`：

```python
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Tuple

OBSERVATION_INTERVAL_SEC = 1.0
MIN_COVERAGE_RATIO = 0.90
DISCONTINUITY_RESET_SEC = 30.0


@dataclass(frozen=True)
class StrategyState:
    ready: bool
    center_bps: Optional[float]
    upper_bps: float
    lower_bps: float
    window_minutes: Optional[int] = None
    warmup_span_sec: Optional[float] = None
    coverage_ratio: Optional[float] = None


class StableBasisStrategy:
    name = "stable_basis"
    requires_observations = False

    def __init__(self, *, center_bps: float, upper_bps: float, lower_bps: float):
        self._state = StrategyState(
            ready=True,
            center_bps=center_bps,
            upper_bps=upper_bps,
            lower_bps=lower_bps,
        )

    def update(self, timestamp: float, premium_bps: float) -> None:
        return None

    def state(self) -> StrategyState:
        return self._state
```

- [ ] **Step 5: 實作 DriftingBasisStrategy 的 timestamp-based deque**

核心狀態必須是：

```python
self.window_minutes = window_minutes
self.window_sec = float(window_minutes * 60)
self.upper_bps = upper_bps
self.lower_bps = lower_bps
self._samples: Deque[Tuple[float, float]] = deque()
self._segment_start_ts: Optional[float] = None
self._last_valid_ts: Optional[float] = None
```

`update()` 行為：

```python
def update(self, timestamp: float, premium_bps: float) -> None:
    if not (math.isfinite(timestamp) and math.isfinite(premium_bps)):
        return

    if (self._last_valid_ts is not None
            and timestamp - self._last_valid_ts > DISCONTINUITY_RESET_SEC):
        self._samples.clear()
        self._segment_start_ts = None

    if self._segment_start_ts is None:
        self._segment_start_ts = timestamp

    self._last_valid_ts = timestamp
    self._samples.append((timestamp, premium_bps))

    cutoff = timestamp - self.window_sec
    while self._samples and self._samples[0][0] <= cutoff:
        self._samples.popleft()
```

`state()` 必須用 segment elapsed + valid count 判斷 readiness，而不是要求 deque 第一筆剛好等於 `t-W`：

```python
def state(self) -> StrategyState:
    if self._last_valid_ts is None or self._segment_start_ts is None:
        return StrategyState(
            ready=False, center_bps=None,
            upper_bps=self.upper_bps, lower_bps=self.lower_bps,
            window_minutes=self.window_minutes,
            warmup_span_sec=0.0, coverage_ratio=0.0,
        )

    span = max(self._last_valid_ts - self._segment_start_ts, 0.0)
    expected = self.window_sec / OBSERVATION_INTERVAL_SEC
    coverage = min(len(self._samples) / expected, 1.0)
    ready = span >= self.window_sec and coverage >= MIN_COVERAGE_RATIO
    center = statistics.median(v for _, v in self._samples) if ready else None
    return StrategyState(
        ready=ready,
        center_bps=center,
        upper_bps=self.upper_bps,
        lower_bps=self.lower_bps,
        window_minutes=self.window_minutes,
        warmup_span_sec=span,
        coverage_ratio=coverage,
    )
```

- [ ] **Step 6: Run strategy tests**

```bash
python3 -m pytest tests/test_strategy.py -q
```

Expected: PASS。

- [ ] **Step 7: 加入 restart semantics 的 direct test**

```python
def test_new_drifting_instance_never_restores_old_state():
    old = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    feed_seconds(old, 1000.0, 61, lambda i: 2.0)
    assert old.state().ready is True

    restarted = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
    assert restarted.state().ready is False
    assert restarted.state().center_bps is None
```

- [ ] **Step 8: Run strategy + full suite**

```bash
python3 -m pytest tests/test_strategy.py -q
python3 -m pytest -q
```

Expected: PASS。

- [ ] **Step 9: Commit**

```bash
git add entropy_arb/strategy.py tests/test_strategy.py
git commit -m "feat: add basis strategy library"
```

---

### Task 3: Config migration + strategy factory + `stable_basis` Engine regression

**Files:**
- Modify: `entropy_arb/config.py`
- Modify: `entropy_arb/strategy.py`
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_engine.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class StrategyConf:
    name: str
    upper_bps: float
    lower_bps: float
    center_bps: float | None = None
    window_minutes: int | None = None
```

`Config` 移除 `midline_bps / upper_bps / lower_bps` 三個 top-level fields，並新增唯一欄位：

```python
strategy: StrategyConf
```

- Factory signature 固定為 `build_strategy(conf: StrategyConf)`；完整分支實作見本 Task Step 6。

- Engine holds:

```python
self.strategy = build_strategy(cfg.strategy)
```

- `_scan()` captures one immutable `StrategyState` snapshot and returns it with the candidate so trades CSV records the exact center used for that decision.

- [ ] **Step 1: 改寫 config test fixtures，先寫新的 failing validation tests**

將 `tests/test_config.py::MINIMAL` 改為：

```python
MINIMAL = """
strategy:
  name: stable_basis
  params:
    center_bps: 5.0
    upper_bps: 4.0
    lower_bps: 3.0
"""
```

新增：

```python
def test_stable_strategy_config_loads():
    cfg = load(MINIMAL)
    assert cfg.strategy.name == "stable_basis"
    assert cfg.strategy.center_bps == 5.0
    assert cfg.strategy.upper_bps == 4.0
    assert cfg.strategy.lower_bps == 3.0
    assert cfg.strategy.window_minutes is None


def test_drifting_strategy_config_loads():
    cfg = load("""
strategy:
  name: drifting_basis
  params:
    window_minutes: 60
    upper_bps: 3.0
    lower_bps: 3.5
""")
    assert cfg.strategy.name == "drifting_basis"
    assert cfg.strategy.window_minutes == 60
    assert cfg.strategy.center_bps is None


def test_unknown_strategy_rejected():
    expect_error("""
strategy:
  name: unknown_basis
  params:
    upper_bps: 3
    lower_bps: 3
""", "unknown strategy")


def test_strategy_specific_params_rejected():
    expect_error("""
strategy:
  name: drifting_basis
  params:
    center_bps: 1
    window_minutes: 60
    upper_bps: 3
    lower_bps: 3
""", "center_bps")
    expect_error("""
strategy:
  name: stable_basis
  params:
    center_bps: 1
    window_minutes: 60
    upper_bps: 3
    lower_bps: 3
""", "window_minutes")


def test_legacy_thresholds_get_actionable_migration_error():
    expect_error("""
thresholds:
  midline_bps: -1
  upper_bps: 3
  lower_bps: 3.5
""", "strategy:")


def test_nonfinite_strategy_values_rejected():
    expect_error("""
strategy:
  name: stable_basis
  params:
    center_bps: .nan
    upper_bps: 3
    lower_bps: 3
""", "finite")
```

- [ ] **Step 2: Run config tests，確認 FAIL**

```bash
python3 -m pytest tests/test_config.py -q
```

Expected: FAIL，因目前 schema 仍要求 `thresholds:`。

- [ ] **Step 3: 在 `config.py` 新增 StrategyConf 並改 Config**

```python
@dataclass(frozen=True)
class StrategyConf:
    name: str
    upper_bps: float
    lower_bps: float
    center_bps: Optional[float] = None
    window_minutes: Optional[int] = None
```

將 `Config` 的：

```python
midline_bps: float
upper_bps: float
lower_bps: float
```

替換成：

```python
strategy: StrategyConf
```

- [ ] **Step 4: 改 YAML schema 與 parser**

Top-level schema 改為：

```python
"strategy": {
    "name": str,
    "params": dict,
},
```

在 `_validate()` 補上 mapping type：

```python
elif want is dict:
    if not isinstance(val, dict):
        raise ConfigError(f"'{here}' must be a mapping")
```

在 `load_config()` 讀 `_validate()` 前先攔 legacy：

```python
if "thresholds" in raw:
    raise ConfigError(
        "legacy 'thresholds:' config is no longer supported; use:\n"
        "strategy:\n"
        "  name: stable_basis\n"
        "  params:\n"
        "    center_bps: <old midline_bps>\n"
        "    upper_bps: <old upper_bps>\n"
        "    lower_bps: <old lower_bps>"
    )
```

新增 `import math`，並實作 `_parse_strategy(raw: dict) -> StrategyConf`；不要只做寬鬆 dict pass-through：

```python
def _finite_number(params: dict, key: str, path: str) -> float:
    if key not in params:
        raise ConfigError(f"'{path}.{key}' is required")
    value = params[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"'{path}.{key}' must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ConfigError(f"'{path}.{key}' must be finite")
    return value


def _parse_strategy(raw: dict) -> StrategyConf:
    node = raw.get("strategy")
    if not isinstance(node, dict):
        raise ConfigError("'strategy' is required and must be a mapping")

    name = node.get("name")
    params = node.get("params")
    if name not in ("stable_basis", "drifting_basis"):
        raise ConfigError(f"unknown strategy {name!r}")
    if not isinstance(params, dict):
        raise ConfigError("'strategy.params' must be a mapping")

    if name == "stable_basis":
        allowed = {"center_bps", "upper_bps", "lower_bps"}
    else:
        allowed = {"window_minutes", "upper_bps", "lower_bps"}

    unknown = set(params) - allowed
    if unknown:
        key = sorted(unknown)[0]
        raise ConfigError(f"unknown/forbidden strategy parameter 'strategy.params.{key}'")

    upper = _finite_number(params, "upper_bps", "strategy.params")
    lower = _finite_number(params, "lower_bps", "strategy.params")
    if upper <= 0 or lower <= 0:
        raise ConfigError("strategy upper_bps and lower_bps must be > 0")

    if name == "stable_basis":
        center = _finite_number(params, "center_bps", "strategy.params")
        return StrategyConf(
            name=name,
            center_bps=center,
            upper_bps=upper,
            lower_bps=lower,
        )

    if "window_minutes" not in params:
        raise ConfigError("'strategy.params.window_minutes' is required")
    window = params["window_minutes"]
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        raise ConfigError("strategy.params.window_minutes must be a positive integer")
    return StrategyConf(
        name=name,
        window_minutes=window,
        upper_bps=upper,
        lower_bps=lower,
    )
```

在 `load_config()` 內先完成 legacy check、再 `_validate(raw, _SCHEMA)`、再：

```python
strategy_conf = _parse_strategy(raw)
```

後續建立 `Config` instance 時直接使用這個 `strategy_conf`。

- [ ] **Step 5: 在 Config return 中只填 `strategy=strategy_conf`**

`Config` constructor 移除舊的 `midline_bps`、`upper_bps`、`lower_bps` 三個 keyword，改為：

```python
strategy=strategy_conf,
```

- [ ] **Step 6: 在 `strategy.py` 加入 factory**

```python
def build_strategy(conf):
    if conf.name == "stable_basis":
        return StableBasisStrategy(
            center_bps=conf.center_bps,
            upper_bps=conf.upper_bps,
            lower_bps=conf.lower_bps,
        )
    if conf.name == "drifting_basis":
        return DriftingBasisStrategy(
            window_minutes=conf.window_minutes,
            upper_bps=conf.upper_bps,
            lower_bps=conf.lower_bps,
        )
    raise ValueError(f"unknown strategy {conf.name!r}")
```

Config validation 應保證正常 startup 不會走到最後一個 branch；最後的 `ValueError` 是 defensive guard。

- [ ] **Step 7: 先新增 `tests/test_engine.py` 的 stable characterization tests**

把 `tests/test_engine.py::make_cfg()` 改成可同時建立兩種 strategy，但預設仍為 stable；這個 helper 之後 Task 4 直接重用：

```python
def make_cfg(
    midline=5.0,
    upper=4.0,
    lower=3.0,
    *,
    hedge_venue="lighter-rh",
    recorder_enabled=True,
    strategy_name="stable_basis",
    window_minutes=60,
):
    if strategy_name == "stable_basis":
        strategy_yaml = f"""
strategy:
  name: stable_basis
  params:
    center_bps: {midline}
    upper_bps: {upper}
    lower_bps: {lower}
"""
    elif strategy_name == "drifting_basis":
        strategy_yaml = f"""
strategy:
  name: drifting_basis
  params:
    window_minutes: {window_minutes}
    upper_bps: {upper}
    lower_bps: {lower}
"""
    else:
        raise ValueError(strategy_name)

    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(strategy_yaml + f"""
execution:
  premium_persist_sec: 0.0
recorder:
  enabled: {str(recorder_enabled).lower()}
  csv: {os.path.join(tempfile.gettempdir(), "engine-minutes.csv")}
""")
    f.close()
    return load_config(
        f.name,
        NO_ENV,
        symbol="SNDK",
        hedge_venue=hedge_venue,
    )
```

`make_engine(**cfg_kw)` 保持目前建立 `StubVenue` 的方式，只改成把 `**cfg_kw` 傳給新的 `make_cfg()`。

新增明確公式回歸：

```python
def test_stable_strategy_preserves_legacy_hurdle_math():
    eng = make_engine(midline=5.0, upper=4.0, lower=3.0)
    state = eng.strategy.state()
    e, h = eng.entropy, eng.hedge
    approx(eng._eff_threshold(h, e, state), 9.0)
    approx(eng._eff_threshold(e, h, state), -2.0)
    approx(
        eng._eff_threshold(h, e, state)
        + eng._eff_threshold(e, h, state),
        7.0,
    )
```

現有三個 `_scan()` tests（sell / quiet / buy）保留相同 BBO 與期待 direction，作為 end-to-end regression。

- [ ] **Step 8: Run config + engine tests，確認 Engine 因舊 cfg fields FAIL**

```bash
python3 -m pytest tests/test_config.py tests/test_engine.py -q
```

Expected: config 新 tests 可逐步 PASS，但 Engine 仍因 `cfg.midline_bps` / `cfg.upper_bps` / `cfg.lower_bps` references 失敗。

- [ ] **Step 9: Wire `Engine.__init__()` to strategy**

Import：

```python
from .strategy import StrategyState, build_strategy
```

在 `Engine.__init__()`：

```python
self.strategy = build_strategy(cfg.strategy)
```

- [ ] **Step 10: 將 threshold math 改成 StrategyState snapshot**

將 `_eff_threshold()` 改成：

```python
def _eff_threshold(self, buy, sell, state: StrategyState) -> float:
    if not state.ready or state.center_bps is None:
        raise RuntimeError("strategy state is not ready")
    if sell.key == "entropy":
        base = state.center_bps + state.upper_bps
    else:
        base = state.lower_bps - state.center_bps
    return base + self._inv_add_bps(buy, sell)
```

將 `_plan()` 改為接受 `state`：

```python
def _plan(self, buy, sell, cap_notional: float, state: StrategyState):
    return plan_arb(
        buy.book,
        sell.book,
        threshold_bps=self._eff_threshold(buy, sell, state),
        buy_fee_bps=buy.fee_bps,
        sell_fee_bps=sell.fee_bps,
        take_fraction=self.cfg.take_fraction,
        cap_notional=cap_notional,
        min_base=self._min_base,
        min_notional=self._min_notional,
        size_step=self._step,
    )
```

- [ ] **Step 11: `_scan()` 一次只抓一份 immutable strategy state**

在 `_scan()` 最前面：

```python
state = self.strategy.state()
if not state.ready or state.center_bps is None:
    self._armed["sell_entropy"] = None
    self._armed["buy_entropy"] = None
    return None
```

後續所有 `_plan()` 都傳同一個 `state`。

`best` 改成：

```python
best = (buy, sell, plan, state)
```

`_evaluate()` 改成：

```python
buy, sell, plan, state = best
await self._vlock(buy.key).acquire()
await self._vlock(sell.key).acquire()
t = asyncio.create_task(self._execute_locked(buy, sell, plan, state))
self._exec_tasks.add(t)
t.add_done_callback(self._exec_tasks.discard)
await asyncio.shield(t)
```

`_execute_locked()` / `_execute()` / `_log_csv()` 傳遞同一個 `StrategyState`；不得在 fill settle 後重新讀 current center 來記錄 entry center。最終 signatures 固定為：

最終 signatures 固定為：

```text
_execute_locked(buy, sell, plan: ArbPlan, strategy_state: StrategyState) -> None
_execute(buy, sell, plan: ArbPlan, strategy_state: StrategyState) -> bool
_log_csv(direction, buy, sell, plan: ArbPlan, ok, bfill, sfill, bstatus,
         sstatus, fill_edge, inv_bps, strategy_state: StrategyState) -> None
```

在 `_execute_locked()` 將 snapshot 原樣傳給 `_execute()`：

```python
unresolved = await self._execute(buy, sell, plan, strategy_state)
```

在 `_execute()` 原本 `_log_csv` call 的最後加：

```python
strategy_state
```

所有其他 execution / settle / hedge body 保持原樣。

- [ ] **Step 12: trades CSV 保留 header，但寫 signal-time center**

`CSV_HEADER` 的 `midline_bps` 欄位名稱先保留，避免不必要 schema churn。

`_log_csv()` 寫：

```python
f"{strategy_state.center_bps:.3f}"
```

而不是 `self.cfg.midline_bps`。

- [ ] **Step 13: 更新 startup log 的 stable case**

Stable 啟動至少輸出：

```text
strategy=stable_basis center=-1.00bps band=[-4.50,+2.00]
No automatic strategy selection.
```

Band 必須依 `center-lower` / `center+upper` 計算。

- [ ] **Step 14: Run focused tests**

```bash
python3 -m pytest tests/test_config.py tests/test_strategy.py tests/test_engine.py -q
```

Expected: PASS；既有 sell / quiet / buy / position-cap / reference lifecycle tests 仍 PASS。

- [ ] **Step 15: Commit**

```bash
git add entropy_arb/config.py entropy_arb/strategy.py entropy_arb/engine.py tests/test_config.py tests/test_engine.py
git commit -m "refactor: route stable basis through strategy layer"
```

---

### Task 4: Wire `drifting_basis` live observation、warm-up 與 gap reset

**Files:**
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_engine.py`

**Interfaces:**
- New Engine helper:

```python
def _sample_strategy_observation(self, now: float | None = None) -> bool:
    """Feed one valid ~1 Hz mid-premium observation when required."""
```

- New Engine task signature: `async def _strategy_observation_loop(self) -> None`；完整 loop body 見本 Task Step 6。

- `stable_basis.requires_observations = False`，因此不得新增 stable 的 1 Hz strategy wakeup path。

- [ ] **Step 1: 寫 unready scan blocking test**

```python
def test_unready_drifting_strategy_blocks_scan_and_clears_arming():
    eng = make_engine(strategy_name="drifting_basis", window_minutes=1)
    eng.entropy.set_book(100.14, 100.16)
    eng.hedge.set_book(99.99, 100.01)
    eng._armed["sell_entropy"] = 123.0
    assert eng._scan(1000.0) is None
    assert eng._armed["sell_entropy"] is None
    assert eng._armed["buy_entropy"] is None
```

這裡直接重用 Task 3 已擴充的 `make_engine(**cfg_kw)`；不要再建立第二個 `make_engine_from_cfg()` helper。

- [ ] **Step 2: 寫 observation sampling test**

```python
def test_drifting_observation_uses_mid_premium_and_fresh_books():
    eng = make_engine(strategy_name="drifting_basis", window_minutes=1)
    eng.entropy.set_book(100.10, 100.20)
    eng.hedge.set_book(99.90, 100.00)
    assert eng._sample_strategy_observation(now=1000.0) is True
    state = eng.strategy.state()
    assert state.ready is False
    assert state.coverage_ratio > 0
```

另寫 stale case，完整測試如下：

```python
def test_drifting_observation_ignores_stale_books():
    eng = make_engine(strategy_name="drifting_basis", window_minutes=1)
    eng.entropy.set_book(100.10, 100.20)
    eng.hedge.set_book(99.90, 100.00)
    before = eng.strategy.state()
    eng.entropy.book.alive_ts = time.time() - eng.cfg.staleness_sec - 1.0
    assert eng._sample_strategy_observation(now=1000.0) is False
    assert eng.strategy.state() == before
```

`tests/test_engine.py` 已 import `time` module 或將目前 `__import__("time")` 用法整理成正式 `import time`。

- [ ] **Step 3: 寫 stable 不需要 observation loop 的 lifecycle test**

直接 assert：

```python
eng = make_engine(strategy_name="stable_basis")
assert eng.strategy.requires_observations is False
```

再加入一個最小 capability test，鎖住 stable 不會被 observer helper 餵資料：

```python
def test_stable_strategy_observer_helper_is_noop():
    eng = make_engine(strategy_name="stable_basis")
    eng.entropy.set_book(100.10, 100.20)
    eng.hedge.set_book(99.90, 100.00)
    before = eng.strategy.state()
    assert eng._sample_strategy_observation(now=1000.0) is False
    assert eng.strategy.state() == before
```

`_run_inner()` 的 task wiring 由 Step 7 的明確 `if self.strategy.requires_observations:` conditional 保證；不額外建立 brittle 的 asyncio task-name introspection test。

- [ ] **Step 4: Run engine tests，確認 FAIL**

```bash
python3 -m pytest tests/test_engine.py -q
```

Expected: FAIL，因 observer helper/loop 尚未存在。

- [ ] **Step 5: 實作 `_sample_strategy_observation()`**

```python
def _sample_strategy_observation(self, now=None) -> bool:
    if not self.strategy.requires_observations:
        return False
    now = time.time() if now is None else now
    cfg = self.cfg
    if not (self.entropy.book.is_fresh(cfg.staleness_sec)
            and self.hedge.book.is_fresh(cfg.staleness_sec)):
        return False
    e_bid, e_ask = self.entropy.book.best_bid(), self.entropy.book.best_ask()
    h_bid, h_ask = self.hedge.book.best_bid(), self.hedge.book.best_ask()
    if None in (e_bid, e_ask, h_bid, h_ask):
        return False
    values = calculate_premiums(e_bid, e_ask, h_bid, h_ask)
    before = self.strategy.state()
    self.strategy.update(now, values.premium_bps)
    after = self.strategy.state()
    if after != before:
        self._update_evt.set()
    return True
```

Import：

```python
from .premium import calculate_premiums
```

- [ ] **Step 6: 實作約 1 Hz observer loop**

```python
async def _strategy_observation_loop(self) -> None:
    while not self.stop.is_set():
        try:
            self._sample_strategy_observation()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("strategy observation failed")
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
```

- [ ] **Step 7: 只在 live + drifting 啟動 observer task**

在 `_run_inner()`：

```python
if not self.record_only:
    if self.strategy.requires_observations:
        tasks.append(asyncio.create_task(
            self._strategy_observation_loop(),
            name="strategy-observer",
        ))
    tasks.append(asyncio.create_task(self._strategy_loop(), name="strategy"))
```

`record_only` 不執行 strategy，也不需要 warm-up strategy state；它只收資料。

- [ ] **Step 8: 確認 gap reset 是 Strategy 自己完成，Engine 不複製 30s logic**

不要在 Engine 再寫第二份 `if gap > 30`。Engine 只負責「這秒有沒有 valid observation」，30 秒 discontinuity semantics 完全由 `DriftingBasisStrategy.update()` 管理。

- [ ] **Step 9: Run focused tests**

```bash
python3 -m pytest tests/test_strategy.py tests/test_engine.py -q
```

Expected: PASS。

- [ ] **Step 10: Run full suite**

```bash
python3 -m pytest -q
```

Expected: PASS。

- [ ] **Step 11: Commit**

```bash
git add entropy_arb/engine.py tests/test_engine.py
git commit -m "feat: feed drifting basis from live premium observations"
```

---

### Task 5: Dynamic observability — status、dashboard、trade logging

**Files:**
- Modify: `entropy_arb/engine.py`
- Modify: `entropy_arb/dashboard.py`
- Modify: `tests/test_dashboard.py`
- Modify: `tests/test_engine.py`

**Interfaces:**
- Dashboard/Status only read `eng.strategy.state()`；不得 mutate strategy。
- Stable display: strategy name + fixed center + absolute band。
- Drifting warm-up display: `WARMING_UP` + window + coverage。
- Drifting ready display: current rolling center + current absolute band。

- [ ] **Step 1: 先把 dashboard fixture 改成可選 strategy，並寫 warm-up failing test**

在 `tests/test_dashboard.py` 加 `import pytest`，把 `make_cfg()` / `make_engine()` 改成接受 strategy 參數：

```python
def make_cfg(strategy_name="stable_basis", center=2.0,
             upper=4.0, lower=3.0, window_minutes=60):
    if strategy_name == "stable_basis":
        strategy = f"""
strategy:
  name: stable_basis
  params:
    center_bps: {center}
    upper_bps: {upper}
    lower_bps: {lower}
"""
    else:
        strategy = f"""
strategy:
  name: drifting_basis
  params:
    window_minutes: {window_minutes}
    upper_bps: {upper}
    lower_bps: {lower}
"""
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(strategy)
    f.close()
    return load_config(f.name, NO_ENV, symbol="SNDK", hedge_venue="lighter-rh")


def make_engine(**strategy_kw):
    eng = Engine(make_cfg(**strategy_kw))
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.hedge = StubVenue("hedge", "RH")
    eng.venues = {"entropy": eng.entropy, "hedge": eng.hedge}
    eng.markets_ready = True
    return eng
```

新增 warm-up test：

```python
def test_renders_drifting_warmup_without_fake_center():
    eng = make_engine(strategy_name="drifting_basis", window_minutes=1,
                      upper=3.0, lower=3.5)
    eng.entropy.set_book(100.1, 100.2)
    eng.hedge.set_book(99.9, 100.0)
    for i in range(30):
        eng.strategy.update(1000.0 + i, 1.0)
    state = eng.strategy.state()
    assert state.ready is False
    assert state.coverage_ratio == pytest.approx(0.5)

    out = render(eng)
    assert "drifting_basis" in out
    assert "WARMING_UP" in out
    assert "1m" in out
    assert "render error" not in out
```

Warm-up 畫面不得用 provisional median 假裝成正式 numeric center。

- [ ] **Step 2: 寫 dashboard ready center failing test**

```python
def test_renders_ready_drifting_center_and_absolute_band():
    eng = make_engine(strategy_name="drifting_basis", window_minutes=1,
                      upper=3.0, lower=3.5)
    eng.entropy.set_book(100.1, 100.2)
    eng.hedge.set_book(99.9, 100.0)
    for i in range(61):
        eng.strategy.update(1000.0 + i, 1.25)
    assert eng.strategy.state().ready is True

    out = render(eng)
    assert "1.25" in out
    assert "-2.25" in out
    assert "+4.25" in out
    assert "render error" not in out
```

- [ ] **Step 3: 寫 status log 與 read-only observability tests**

在 `tests/test_engine.py` 加 helper：

```python
async def run_one_status_cycle(eng):
    eng.cfg.status_interval_sec = 0.01
    task = asyncio.create_task(eng._status_loop())
    await asyncio.sleep(0.02)
    eng.stop.set()
    await task
```

Stable：

```python
def test_status_reports_stable_strategy(caplog):
    eng = make_engine(strategy_name="stable_basis", midline=-1.0,
                      upper=3.0, lower=3.5)
    eng.entropy.set_book(100.0, 100.1)
    eng.hedge.set_book(100.0, 100.1)
    asyncio.run(run_one_status_cycle(eng))
    assert "strategy=stable_basis" in caplog.text
    assert "center=-1.00" in caplog.text
```

Drifting warm-up：

```python
def test_status_reports_drifting_warmup(caplog):
    eng = make_engine(strategy_name="drifting_basis", window_minutes=1)
    eng.entropy.set_book(100.0, 100.1)
    eng.hedge.set_book(100.0, 100.1)
    eng.strategy.update(1000.0, 0.0)
    before = eng.strategy.state()
    asyncio.run(run_one_status_cycle(eng))
    after = eng.strategy.state()
    assert "strategy=drifting_basis" in caplog.text
    assert "WARMING_UP" in caplog.text
    assert before == after
```

Dashboard 也加相同的 read-only assertion：`before = eng.strategy.state(); render(eng); assert eng.strategy.state() == before`。

- [ ] **Step 4: Run tests，確認 FAIL**

```bash
python3 -m pytest tests/test_dashboard.py tests/test_engine.py -q
```

Expected: FAIL，因 dashboard/status 仍讀舊 `cfg.midline_bps`。

- [ ] **Step 5: 更新 Dashboard `_signal_panel()`**

一開始取：

```python
state = eng.strategy.state()
```

Stable / ready drifting：

```python
center = state.center_bps
low = center - state.lower_bps
high = center + state.upper_bps
```

Warm-up：

```text
strategy=drifting_basis
center=WARMING_UP
window=60m
valid=50.0%
```

Directional table 在 strategy unready 時 hurdle/gap 顯示 `—`，不要算 numeric entry hurdle。

- [ ] **Step 6: 更新 startup log 與 `_status_loop()`**

`_run_inner()` 在 market metadata resolve 後明確輸出 selected strategy。Stable：

```text
strategy=stable_basis center=-1.00bps band=[-4.50,+2.00]
No automatic strategy selection.
```

Drifting：

```text
strategy=drifting_basis window=60m center=WARMING_UP band-offset=[-3.50,+3.00]
No automatic strategy selection.
```

接著更新 `_status_loop()`：

Ready：

```text
strategy=stable_basis center=-1.00 band=-4.50..+2.00
```

或：

```text
strategy=drifting_basis center=+0.42 band=-3.08..+3.42
```

Warm-up：

```text
strategy=drifting_basis WARMING_UP window=60m span=42.3m valid=98.7%
```

不得再讀 `cfg.midline_bps / upper_bps / lower_bps`。

- [ ] **Step 7: 確認 trades CSV 是 decision-time center**

在 `tests/test_engine.py` 直接測 `_log_csv()` 對傳入 snapshot 的語義，不建立多餘 mutable strategy test double：

```python
def test_trade_csv_records_captured_strategy_center(tmp_path):
    from entropy_arb.strategy import StrategyState

    eng = make_engine(strategy_name="stable_basis")
    eng.cfg.trades_csv = str(tmp_path / "trades.csv")
    buy, sell = eng.hedge, eng.entropy
    plan = SimpleNamespace(
        qty=0.1,
        buy_limit=100.0,
        sell_limit=100.1,
        buy_notional=10.0,
        sell_notional=10.01,
        exp_edge_usd=0.01,
        gross_edge_usd=0.01,
        marginal_premium_bps=10.0,
    )
    captured = StrategyState(
        ready=True,
        center_bps=1.25,
        upper_bps=3.0,
        lower_bps=3.5,
    )
    eng._log_csv(
        "sell_entropy", buy, sell, plan, True,
        0.1, 0.1, "filled", "filled", 0.01, 0.0, captured,
    )

    import csv
    with open(eng.cfg.trades_csv) as fh:
        row = next(csv.DictReader(fh))
    assert row["midline_bps"] == "1.250"
```

這個 test 鎖住 `_log_csv()` 必須使用 `_scan()` captured `StrategyState`，不能自行重新讀 current strategy state。

- [ ] **Step 8: Run focused tests**

```bash
python3 -m pytest tests/test_dashboard.py tests/test_engine.py -q
```

Expected: PASS。

- [ ] **Step 9: Commit**

```bash
git add entropy_arb/engine.py entropy_arb/dashboard.py tests/test_dashboard.py tests/test_engine.py
git commit -m "feat: expose strategy state in live observability"
```

---

### Task 6: Config example、CLI/docs migration 與 replay parity contract

**Files:**
- Modify: `config.example.yaml`
- Modify: `main.py`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `entropy_arb/recorder.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_strategy.py`

**Interfaces:**
- User-facing config only supports:

```yaml
strategy:
  name: stable_basis
  params:
    center_bps: -1.0
    upper_bps: 3.0
    lower_bps: 3.5
```

or:

```yaml
strategy:
  name: drifting_basis
  params:
    window_minutes: 60
    upper_bps: 3.0
    lower_bps: 3.5
```

- No automatic strategy selection wording anywhere in runtime docs.

- [ ] **Step 1: 更新 `config.example.yaml`**

移除：

舊的 active `thresholds:` block（`midline_bps`、`upper_bps`、`lower_bps`）整段移除。

預設範例改成 `stable_basis`，因它是現有 production baseline：

```yaml
strategy:
  name: stable_basis
  params:
    center_bps: 0.0
    upper_bps: 4.0
    lower_bps: 4.0
```

在註解中提供 `drifting_basis` alternate example，但不要同時讓兩套 active。

- [ ] **Step 2: 更新 `tests/test_config.py::test_example_config_loads()`**

新增：

```python
assert cfg.strategy.name == "stable_basis"
assert cfg.strategy.center_bps == 0.0
assert cfg.strategy.upper_bps == 4.0
assert cfg.strategy.lower_bps == 4.0
```

- [ ] **Step 3: 更新 `main.py` docstring / help text**

將「collect data, set your thresholds, then go live」改成：

```text
collect data, review the market, select a strategy and parameters in config.yaml,
then go live with small position caps.
```

不要新增 auto-analyzer / auto-selection 的承諾。

- [ ] **Step 4: 更新 recorder module docstring**

保留 `premium / sell_edge / buy_edge` 三個公式，但把：

```text
choose thresholds.midline_bps / upper_bps / lower_bps
```

改成：

```text
support offline market analysis and strategy parameter selection
```

不得暗示 recorder 自己會選 strategy。

- [ ] **Step 5: 更新 README / README.zh-CN**

至少包含：

```text
stable_basis:
  fixed human-selected center

drifting_basis:
  causal rolling-median center
  full-window warm-up
  90% valid coverage
  >30s valid-observation gap resets warm-up
  restart does not restore center state

No automatic market diagnosis or strategy switching in the live bot.
```

中文 README 使用中文說明，但 config key/name 保持英文原樣。

- [ ] **Step 6: 加入 historical replay parity test**

在 `tests/test_strategy.py`：

```python
def test_same_observation_stream_produces_deterministic_replay_state_sequence():
    observations = [
        (1000.0 + i, -2.0 if i < 30 else 2.0)
        for i in range(61)
    ]

    live_style = DriftingBasisStrategy(
        window_minutes=1, upper_bps=3.0, lower_bps=3.5
    )
    replay_style = DriftingBasisStrategy(
        window_minutes=1, upper_bps=3.0, lower_bps=3.5
    )

    live_states = []
    replay_states = []
    for ts, premium in observations:
        live_style.update(ts, premium)
        live_states.append(live_style.state())
    for ts, premium in observations:
        replay_style.update(ts, premium)
        replay_states.append(replay_style.state())

    assert replay_states == live_states
```

這個 test 明確鎖住未來 Analyzer 要重用同一 Strategy implementation 的 contract。

- [ ] **Step 7: Run docs-adjacent focused tests**

```bash
python3 -m pytest tests/test_config.py tests/test_strategy.py tests/test_recorder.py -q
```

Expected: PASS。

- [ ] **Step 8: Commit**

```bash
git add config.example.yaml main.py README.md README.zh-CN.md entropy_arb/recorder.py tests/test_config.py tests/test_strategy.py
git commit -m "docs: document explicit basis strategy selection"
```

---

### Task 7: 全面回歸驗證與完成 gate

**Files:**
- Verify only unless failures expose a real regression.
- Test: all `tests/`

**Interfaces:**
- No new interfaces。
- This task proves Subproject A satisfies the approved design before any Market Analyzer work starts。

- [ ] **Step 1: Run `git diff --check`**

```bash
git diff --check
```

Expected: no output, exit 0。

- [ ] **Step 2: Run strategy/config/premium focused suite**

```bash
python3 -m pytest tests/test_premium.py tests/test_strategy.py tests/test_config.py -q
```

Expected: PASS。

- [ ] **Step 3: Run Engine safety regression suite**

```bash
python3 -m pytest tests/test_engine.py tests/test_book.py -q
```

Expected: PASS；尤其確認：

```text
stable sell/quiet/buy signal regression
inventory ladder
position caps
reference lifecycle
reference failure isolation
shutdown behavior
```

- [ ] **Step 4: Run recorder/reference regression**

```bash
python3 -m pytest tests/test_recorder.py tests/test_reference.py -q
```

Expected: PASS；reference recorder 不得因 Strategy Library 而產生 strategy wakeup dependency。

- [ ] **Step 5: Run dashboard regression**

```bash
python3 -m pytest tests/test_dashboard.py -q
```

Expected: PASS。

- [ ] **Step 6: Run complete suite**

```bash
python3 -m pytest -q
```

Expected: all PASS；記錄實際 test count。

- [ ] **Step 7: Static scope check**

Run：

```bash
grep -R "send_taker\|init_signer\|private_key" -n entropy_arb/strategy.py
```

Expected: no matches。

Run：

```bash
grep -R "thresholds:" -n config.example.yaml README.md README.zh-CN.md
```

Expected: 除非 README 是在「legacy migration example」中明確說明舊格式，否則不應有 active config example。

- [ ] **Step 8: Manual config smoke — stable_basis, record-only**

準備一份新 schema config，Run：

```bash
python3 main.py --record-only --symbol SNDK --hedge lighter-rh --config config.yaml --no-dashboard
```

Expected:

```text
record-only 不需要 credentials
recorder/reference 正常啟動
不送 orders
config 可解析 stable_basis
```

此 smoke 不要求 strategy observer warm-up，因 record-only 不運行 strategy。

- [ ] **Step 9: Credential-free unit-level drifting smoke**

不要 live 下單。用 Python 直接建立 strategy：

```bash
python3 - <<'PY'
from entropy_arb.strategy import DriftingBasisStrategy

s = DriftingBasisStrategy(window_minutes=1, upper_bps=3.0, lower_bps=3.5)
for i in range(61):
    s.update(1_000.0 + i, -1.0 + i / 100.0)
print(s.state())
assert s.state().ready
PY
```

Expected: `ready=True`，center finite，無任何 venue/order dependency。

- [ ] **Step 10: Review changed files against out-of-scope list**

Run：

```bash
git diff --name-only HEAD~6..HEAD
```

人工確認沒有新增：

```text
Market Analyzer
regime detector
auto strategy selection
reference-residual strategy
lead-lag filter
locked-PnL exit
pair-lot state
state persistence
new venue abstraction
```

- [ ] **Step 11: Final commit only if verification required small fixes**

如果 Step 1–10 發現真實 regression 並修正，才 commit：

```bash
git add <only-files-fixed-for-verification>
git commit -m "fix: close strategy library regression gaps"
```

若沒有修正，不建立空 commit。

---

## 完成條件

Subproject A 只有在以下全部成立時才算完成：

- `config.yaml` 可明確選擇 `stable_basis` 或 `drifting_basis`。
- 舊 `thresholds:` schema 會用可執行的 migration 訊息拒絕啟動，不 silent fallback。
- `stable_basis` 對等價參數維持既有 hurdle、direction、plan、qty、limit、firing semantics。
- `drifting_basis` 使用 causal、timestamp-based、約 1 Hz rolling median。
- 60m window 需要完整 60m temporal warm-up 且 coverage >= 90%。
- >30s valid observation gap 清空 drifting history；短 gap 只降低 coverage。
- restart 從空 drifting state 開始，不讀舊 CSV seed。
- unready strategy 不送新的 arbitrage orders，但 feeds / recorder / reference / balance / reconcile 繼續正常。
- Strategy Library 沒有 order API / credential / signer / venue dependency。
- Recorder、live observation、future replay 使用相同 premium 公式。
- trades CSV 記錄的是 signal 當下實際 center，不是 fill settle 後 center。
- Dashboard/status 明確顯示 strategy、ready/warm-up、current center。
- Live bot 沒有任何自動市場判斷或策略切換。
- 完整既有測試 + 新增測試全部 PASS。

## Execution Handoff

實作時有兩種方式：

1. **Subagent-Driven（建議）**：每個 Task 派一個 fresh subagent，Task 間做 review，適合這個多檔案但邊界清楚的 refactor。
2. **Inline Execution**：在同一個 session 用 `superpowers:executing-plans` 分批執行並設 checkpoint。
