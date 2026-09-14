# Dynamic Threshold Shadow Mode Design

狀態：Draft，供使用者檢閱。此文件只描述第一階段 Shadow 模式，不授權動態門檻進入實盤下單。

## 目標

為每一組 `symbol / hedge` 建立獨立的 rolling threshold calculator，使用已完成的分鐘資料計算動態 `midline`、`upper`、`lower`，並記錄結果供研究。第一階段的計算結果只觀察，不改變現有策略行為。

## 非目標

- 不在第一階段讓動態門檻控制下單。
- 不修改現有固定 `thresholds:` 的語意或數值。
- 不改變訂單大小、滑點、cooldown、position limit、reconciliation 或 staleness 風控。
- 不保存每秒原始行情或完整 rolling window。
- 不修改 `tools/analyze.py` 的既有輸出定義。
- 不在本階段實作 canary 或自動切換到動態門檻。

## 已確認的設計決策

- 模式：Shadow。
- 資料來源：`MinuteRecorder` 完成的一分鐘資料列。
- 計算模組：新增 `entropy_arb/dynamic_thresholds.py`。
- `engine.py` 只負責建立、串接與關閉計算器，不實作計算公式。
- rolling window：12 小時。
- 更新頻率：15 分鐘。
- 暖機條件：至少 12 小時窗口預期資料的 80%，即至少 576 個有效分鐘。
- percentile：P90。
- 最低門檻：`floor_bps`，預設 1.0 bps。
- 每個交易對使用自己的狀態，不跨市場共享。

## 架構與資料流

```text
MinuteRecorder
    │ completed minute row
    ▼
DynamicThresholdController
    ├─ per-pair rolling history
    ├─ quality gate
    ├─ median / P90 calculation
    └─ threshold snapshot writer
            │
            ▼
logs/engine/dynamic-thresholds-<symbol>-<hedge>.csv
```

### `entropy_arb/recorder.py`

保留目前分鐘資料的產生方式。每次成功完成並寫出一列 minute row 後，呼叫一個可選的 callback，將同一列傳給 calculator。callback 不應建立新的行情連線，也不應改變 recorder 的採樣與 CSV 格式。

### `entropy_arb/dynamic_thresholds.py`

提供獨立、可測試的 controller／estimator：

- 接收 completed minute row。
- 維護最近 12 小時的有效資料。
- 依 15 分鐘更新邊界計算 snapshot。
- 輸出 `warming_up`、`valid` 或 `frozen` 狀態。
- 將結果寫入交易對專用的 engine log CSV。

它不應 import 下單執行器，也不應直接修改 `Config` 中的固定門檻。

### `entropy_arb/engine.py`

只做生命週期與接線：

- 根據 config 建立 Shadow controller。
- 將 controller callback 傳給同一個 `MinuteRecorder`。
- 啟動與停止時正確 flush／close controller。
- 保持既有策略讀取 `cfg.midline_bps`、`cfg.upper_bps`、`cfg.lower_bps` 的路徑不變。
- Shadow 模式必須使用同一個 recorder；若 `recorder.enabled` 關閉，應在啟動時明確報錯，不另開第二套行情連線。

## 計算規則

只使用符合目前 recorder 資料品質要求的 minute row：

```text
samples >= 10
```

對每一個 12 小時窗口：

```text
midline = median(premium_close_bps)

sell_room = sell_edge_max_bps - midline - fees_bps
buy_room  = buy_edge_max_bps + midline - fees_bps

raw_upper = P90(sell_room)
raw_lower = P90(buy_room)

upper = max(floor_bps, raw_upper)
lower = max(floor_bps, raw_lower)
```

`fees_bps` 來自固定設定，不由 calculator 自動估算或修改。

### 暖機與品質

- 啟動時從目前交易對的既有 minute CSV 載入最近 12 小時，作為 seed。
- 若沒有既有檔案，則從新的 completed rows 開始累積。
- 有效資料少於 576 分鐘時，狀態為 `warming_up`。
- 每次更新前重新檢查窗口覆蓋率與最新資料狀態。
- 已經有有效結果後，若窗口品質不合格或出現不可接受缺口，狀態改為 `frozen`，保留最後有效值並在品質不合格期間停止更新；品質恢復後才可回到 `valid`。
- 不使用 0、目前固定門檻或其他猜測值填補動態結果。
- Shadow 狀態異常時，現有固定策略仍照常使用固定門檻；動態結果永遠不會偷偷接管下單。
- `warming_up` 時門檻欄位保持空值；`frozen` 時保留最後有效門檻並以狀態欄位區分。

## 設定檔

在 `config.yaml` 增加獨立區塊：

```yaml
dynamic_thresholds:
  mode: shadow
  window_hours: 12
  update_minutes: 15
  percentile: 90
  floor_bps: 1.0
  min_coverage_pct: 80
  seed_from_csv: true
```

安全邊界：

- `mode: off`：完全不建立 controller。
- `mode: shadow`：計算與記錄，但不影響策略。
- 第一階段不接受或實作自動 live／canary 模式。
- 原有 `thresholds:` 區塊仍是實盤策略唯一使用的門檻來源。

## Snapshot CSV

每個交易對一個檔案，放在 `logs/engine/`：

```text
dynamic-thresholds-<symbol>-<hedge>.csv
```

欄位：

```text
symbol
hedge
calculated_at_utc
window_start_utc
window_end_utc
status
valid_minutes
coverage_pct
gap_count
midline_bps
raw_upper_bps
raw_lower_bps
upper_bps
lower_bps
midline_change_bps
upper_change_bps
lower_change_bps
reason
```

每 15 分鐘最多一列。只保存結果與品質資訊，不保存完整 rolling window。更新邊界應避免同一個窗口重複寫入；重啟後可從既有 minute CSV seed，但不應重寫歷史 snapshot。

## 測試與驗收

新增 calculator 的單元測試，並補上必要的 recorder／engine wiring 測試：

- 固定輸入下 median、P90 與 floor 結果正確。
- 每個交易對的 rolling state 互不污染。
- 不足 576 筆時保持 `warming_up`。
- 缺口或品質不合格時進入 `frozen` 並保留最後有效結果。
- 可從既有 CSV seed，且不重複寫歷史 snapshot。
- 僅在 15 分鐘更新邊界產生 snapshot。
- `mode: off` 時不建立 controller。
- `mode: shadow` 時固定 `Config` 門檻不變，策略下單判斷路徑不變。
- snapshot 欄位、每交易對檔名與 `logs/engine` 路徑正確。

既有測試套件必須保持通過；測試不連接交易所、不使用憑證、不送出訂單。

## 部署與觀察順序

1. 先實作 calculator、recorder callback、engine wiring 與 config parsing。
2. 執行單元測試與現有測試。
3. 使用已下載的 EC2 CSV 做離線重播，核對 12 小時／15 分鐘結果。
4. 在本機以 Shadow 模式觀察輸出格式。
5. 明確檢查後再上傳 EC2，先觀察至少 24 小時。
6. 第一階段不修改實盤固定門檻，也不啟用動態下單。

## 未來延伸（不在本次實作）

若 Shadow 資料證明門檻穩定，才另行設計 canary：包括單一交易對 allowlist、門檻變動上限、stale freeze 行為、開倉 fail-closed 規則，以及獨立的 live authorization gate。
