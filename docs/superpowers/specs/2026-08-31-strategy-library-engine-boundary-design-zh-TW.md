# Strategy Library（策略庫）+ Engine Boundary（引擎邊界）設計

日期：2026-08-31  
狀態：待使用者審閱後進入實作  
範圍：僅限子專案 A — Strategy Library（策略庫）+ Engine boundary（引擎邊界）  
目標分支：`feature/p2-persistence-research`

## 1. 目的

導入一個精簡的 Strategy Library（策略庫），讓實盤機器人可以從 config 明確指定並執行一套策略，同時保留既有的 execution（執行）、hedging（補對沖）、reconciliation（持倉核對）、rate-limit（限流）、outage handling（交易所異常處理）、sizing（下單規模）、inventory（庫存控制）、recorder（資料記錄）以及 reference（參考價格）基礎設施。

Version 1 僅支援兩個策略模板：

- `stable_basis`：由 config 提供固定的 basis center（基差中樞）。
- `drifting_basis`：使用實盤歷史 premium observations（溢價觀測值），以因果式 rolling median（滾動中位數）估計會漂移的 basis center。

本子專案**不包含**自動市場分類、自動策略選擇、自動策略切換，也不允許 Strategy 自行送出訂單。未來的離線 Market Analyzer（市場分析器）會在歷史資料上比較相同的 Strategy Library 實作，再產生 config 建議供人工審核。

## 2. 設計原則

1. **Engine = 如何安全地交易；Strategy = 目前合理的 basis center 在哪裡，以及這個估計是否已準備好拿來交易。**
2. Strategy code 不得知道 API credentials（API 憑證）、signers（簽章器）、account balances（帳戶餘額）、venue rate limits（交易所限流）、order APIs（下單 API）、reconciliation（持倉核對）或 hedging（補對沖）。
3. Strategy code 不得送出或排程訂單。
4. 既有 `plan_arb()` 的 depth walk（深度掃描）與 `ArbPlan` 繼續作為 execution planning（執行規劃）的核心 primitive（基礎單元）。
5. Inventory surcharge（庫存加價）、position caps（持倉上限）、sizing（下單規模）、persistence（訊號持續條件）、cooldown（冷卻時間）、staleness checks（行情過舊檢查）、venue outage handling（交易所異常處理）、rate limits（限流）、emergency hedging（緊急補對沖）與 reconciliation（持倉核對）仍由 Engine 負責。
6. `stable_basis` 的重構必須在等價參數下保留現有 live signal（實盤訊號）行為。
7. Live 與 historical replay（歷史重播）必須使用同一份 Strategy Library 實作。Market Analyzer 不得另外維護一套近似的策略邏輯。
8. Version 1 刻意只改 center model（中樞模型）。Exit behavior（退出行為）維持現行的 opposite-signal / reduce-or-flip（反向訊號／減倉或翻倉）邏輯。Locked-PnL exit（鎖定損益退出）與 pair-lot lifecycle（配對 lot 生命週期）屬於後續獨立研究。
9. 本次工作不新增任何 fee logic（手續費邏輯）；現有 Engine / planner 對 fee 的行為原封不動保留。

## 3. 架構邊界

```text
Fresh BBO state（最新且有效的最佳買賣價狀態）
    |
    v
1-second premium observation（1 秒溢價觀測）
    |
    v
Strategy.update(observation)
    |
    v
StrategyState
  - ready
  - center_bps
  - upper_bps
  - lower_bps
    |
    v
Engine._scan()
  - freshness（行情新鮮度）
  - venue readiness（交易所可交易狀態）
  - outage state（異常狀態）
  - locks（鎖）
  - rate limits（限流）
  - persistence（訊號持續）
  - inventory surcharge（庫存加價）
  - position headroom（剩餘持倉空間）
    |
    v
plan_arb()
    |
    v
existing execution / hedge / reconcile
（既有執行 / 補對沖 / 持倉核對）
```

Engine 不得根據 market symbol（市場代號）或 venue（交易所）自行分支選擇策略。策略只能由 config 明確指定。

## 4. Strategy 介面

概念上：

```text
update(timestamp, premium_bps) -> None
state() -> StrategyState
```

`StrategyState` 包含：

```text
ready: bool
center_bps: optional float
upper_bps: float
lower_bps: float
```

Strategy 可以維護自己的歷史狀態，但不得存取 venue 或 order API。

Engine 仍然由 StrategyState 推導兩個方向的 hurdle（門檻）：

```text
SELL entropy / BUY hedge：
    strategy center + upper + 現有 inventory surcharge

BUY entropy / SELL hedge：
    lower - strategy center + 現有 inventory surcharge
```

對 `stable_basis` 而言，精確算式必須與目前 `_eff_threshold()` 的行為等價。

## 5. Premium observation（溢價觀測）語義

Center 描述的是兩個 venue 之間的相對 basis，因此要從 mid-to-mid premium（中價對中價溢價）估計，而不是從單一方向的 executable edge（可成交邊際）估計：

```text
premium_bps = (entropy_mid / hedge_mid - 1) * 10_000
```

Entry planning（進場規劃）仍使用 executable BBO（可成交最佳買賣價）：

```text
sell_edge_bps = (entropy_bid / hedge_ask - 1) * 10_000
buy_edge_bps  = (hedge_bid / entropy_ask - 1) * 10_000
```

Live strategy observation 約以 1 Hz 頻率，從當下最新且 fresh 的 BBO state 取樣。WebSocket 訊息頻率不得對 rolling center 形成權重。

應建立一個共用的 pure calculation helper（純計算函式），統一定義 premium / executable-edge 的計算，讓 Recorder、Strategy observation 與 historical replay 使用完全相同的公式。Strategy 不得去讀 recorder CSV 檔案。

## 6. `stable_basis`

### 假設

Structural basis（結構性基差）在預定交易期間內足夠穩定，因此使用人工選定的 fixed center（固定中樞）是合理的。

### Config

```yaml
strategy:
  name: stable_basis
  params:
    center_bps: -1.0
    upper_bps: 3.0
    lower_bps: 3.5
```

### 行為

- 建構完成後立即 `ready = true`。
- `center_bps` 永遠等於 config 指定值。
- 市場 observations 不會修改 center。
- 現有 persistence、inventory、sizing 與 execution 行為全部留在 Strategy 之外。

### Regression requirement（回歸相容要求）

在 books、positions、sizing、inventory、persistence 與 execution settings 完全相同時，舊版 `midline_bps=X, upper_bps=U, lower_bps=L` 必須與新版 `stable_basis(center_bps=X, upper_bps=U, lower_bps=L)` 產生相同的：

- directional hurdle（方向門檻）
- plan
- direction（方向）
- quantity（數量）
- limits（限價）
- firing behavior（觸發行為）

這次 refactor（重構）不得順便偷偷改善或改變現有策略行為。

## 7. `drifting_basis`

### 假設

Structural basis 會隨時間改變，但變動速度慢於策略想交易的短期偏離。

### Config

```yaml
strategy:
  name: drifting_basis
  params:
    window_minutes: 60
    upper_bps: 3.0
    lower_bps: 3.5
```

Version 1 僅支援 `rolling_median`。不包含 EWMA、Kalman filter、HMM 或自動模型選擇。

### Causal center definition（因果式中樞定義）

在時間 `t`、window（視窗）為 `W` 時：

```text
center_t = median(premium_i for observations where t - W < t_i <= t)
```

要求：

- 只能使用時間 `t` 當下或之前已經可取得的資訊。
- Window 必須依 timestamp（時間戳）計算，不得依 row count（資料列數）計算。
- 不允許使用未來資料做 interpolation（插值）。
- 不使用 reference / oracle / index / mark series。
- 不使用方向性的 `sell_edge` 或 `buy_edge` series 估計 center。
- 超出時間 window 的 observations 必須被移除。

## 8. Warm-up（暖機）與 readiness（可交易狀態）

`drifting_basis` 在 center 尚未擁有完整且足夠覆蓋的 window 前，不得交易。

Readiness 必須同時滿足：

1. Observation history 已涵蓋完整的設定時間 window。
2. 該 window 內 valid observation coverage（有效觀測覆蓋率）至少達理論 1 Hz 樣本數的 90%。
3. Engine 評估 signal 的當下，最新 observation 仍然有效 / fresh。

以 `window_minutes: 60` 為例：

```text
expected observations ~= 3600
minimum valid coverage ~= 3240
```

Coverage 是 data-quality gate（資料品質門檻），不是 alpha parameter（Alpha 參數）。Version 1 固定為 90%，不開放到 config 調整。

當 `ready = false` 時，feeds、recorder、reference recorder、balance / reconciliation、status 都照常運作；只有新的策略套利 execution 被禁止。

Status 應顯示 warm-up 進度，但不能造成 log spam，例如：

```text
strategy=drifting_basis status=WARMING_UP coverage=42.3/60.0m valid=98.7%
```

當狀態首次轉為 ready 時，只記錄一次明確事件，內容需包含 window 與目前 center。

## 9. Missing data（缺失資料）與 discontinuities（不連續）

短暫的 missing intervals 不會 reset Strategy。它們只會降低 coverage，並可能暫時阻止 readiness。

真正的 continuous observation discontinuity（連續觀測中斷）會清空 rolling history，並要求重新完成完整 warm-up。Version 1 將真正的不連續定義為：**超過 30 秒沒有任何 valid premium observation**。

30 秒門檻是固定的 safety constant（安全常數），不是策略調參參數。

```text
1-3 秒 jitter / 短暫缺失樣本
    -> 保留 history；coverage 略微下降

>30 秒沒有 valid observation
    -> 清空 rolling history
    -> ready = false
    -> 從第一筆新的 valid observation 重新開始 warm-up
```

這可以避免把 outage 前與 outage 後的 observations 拼接成一段誤導性的連續 rolling center。

## 10. Restart（重啟）行為

Version 1 不把 Strategy state 持久化到磁碟。

每次 process restart：

- `stable_basis` 直接從 config 取得 center，立即 ready。
- `drifting_basis` 從空 history 開始，重新完成一次完整 warm-up。

Version 1 啟動時不會讀取舊 recorder data 來預先填入 rolling center。

## 11. Config schema 與 Strategy Factory（策略工廠）

舊版最上層的 `thresholds:` 區塊由明確的 `strategy:` 區塊取代。

唯一支援的策略名稱：

```text
stable_basis
drifting_basis
```

未知名稱一律視為 startup error（啟動錯誤）。不允許 fallback（降級替代）或自動選擇。

### `stable_basis` validation

必填：

```text
center_bps: finite number（有限數值）
upper_bps: positive finite number（正的有限數值）
lower_bps: positive finite number（正的有限數值）
```

禁止出現 `window_minutes`。

### `drifting_basis` validation

必填：

```text
window_minutes: positive integer（正整數）
upper_bps: positive finite number（正的有限數值）
lower_bps: positive finite number（正的有限數值）
```

禁止出現 `center_bps`。

未知參數一律報錯。

### Legacy config（舊版 config）處理

刻意不長期支援雙 schema。完成 migration（遷移）後，如果偵測到舊版 `thresholds:`，程式啟動失敗，並清楚顯示如何轉成等價的 `stable_basis` 格式。程式不得偷偷自動轉換或使用預設值。

## 12. Startup 與 status observability（可觀測性）

每次 live start 都必須明確顯示所選 Strategy，並明確顯示目前**沒有**自動策略選擇。

Stable 範例：

```text
PAIR ENTROPY(SNDK)-RH(SNDK)
strategy=stable_basis center=-1.00bps band=[-3.50,+3.00]
No automatic strategy selection.
```

Drifting warm-up 範例：

```text
PAIR ENTROPY(SNDK)-RH(SNDK)
strategy=drifting_basis window=60m center=WARMING_UP band=[-3.50,+3.00]
No automatic strategy selection.
```

Status / dashboard 應顯示目前 strategy name、readiness 與 current center。Dashboard 只負責 observability，不得修改 Strategy state。

## 13. Historical replay（歷史重播）相容性

未來的 Market Analyzer 會按照時間順序，把歷史 premium observations 餵給同一套 Strategy Library。

Replay 要求：

- 相同的 `update()` semantics（語義）。
- 相同的 timestamp-based window。
- 相同的 warm-up 與 90% coverage 要求。
- 相同的 >30 秒 discontinuity reset 行為。
- 相同的 strategy-specific config validation。
- 不得使用未來資料。

正式 replay 不得把時間 window 偷換成固定 row count，例如 `rolling(3600)`。

Strategy Library 必須可以在沒有 venue credentials 或 live order clients 的情況下使用。

## 14. Error handling（錯誤處理）

以下情況啟動直接失敗：

- unknown strategy name（未知策略名稱）
- 缺少必要 strategy parameter
- unknown / forbidden strategy parameter（未知／禁止參數）
- non-finite center / threshold values（非有限數值）
- non-positive thresholds（非正數門檻）
- invalid / non-positive drifting window（無效／非正數 drifting window）
- migration 後仍使用 legacy `thresholds:` config

Runtime（執行期間）：

- invalid / non-finite premium observations 直接忽略。
- 超過 30 秒沒有 valid observation，`drifting_basis` state 必須 reset。
- Strategy 尚未 ready 時，只阻止新的 arbitrage execution，不得停止 feeds、recorder、status、balance 或 reconciliation 基礎設施。

## 15. 不在本子專案範圍內

本子專案**不實作**：

- Market Analyzer
- automatic regime detection（自動 regime 偵測）
- automatic strategy selection / switching（自動策略選擇／切換）
- reference-residual strategy
- lead-lag strategy 或 filter
- adaptive upper / lower thresholds（自適應上下門檻）
- locked-PnL exit
- pair-lot lifecycle
- strategy state persistence
- 啟動時從歷史檔案 seed（預填）strategy state
- EWMA、Kalman、HMM、clustering 或其他 center models
- 新的 venue abstraction
- 與本目的無關的 execution / hedge / reconcile 修改

## 16. 測試要求

至少覆蓋以下測試，才算完成實作：

1. `stable_basis` 在 deterministic book / position / config cases 下能重現目前 threshold logic。
2. `stable_basis` 建構後立即 ready，且 center 永遠保持 config 值。
3. `drifting_basis` 在完整 temporal window 尚未形成前不得 ready。
4. `drifting_basis` 即使 temporal window 已滿，但 coverage <90% 時仍不得 ready。
5. 當 temporal span 與 coverage 同時達標時，`drifting_basis` 變為 ready。
6. Rolling median 只能使用當下與過去 observations。
7. 超過 timestamp window 的 observations 必須正確移除。
8. 短暫 missing intervals 不得 reset history。
9. 超過 30 秒的 gap 必須清空 history 並重新 warm-up。
10. Process restart 不得恢復 drifting state。
11. Invalid / non-finite observations 不得污染 center。
12. Unknown strategy names 與 invalid strategy-specific parameters 必須 validation fail。
13. Legacy `thresholds:` 必須啟動失敗，並給出可操作的 migration message。
14. 對 deterministic observation stream 做 historical replay，必須得到與 live-style sequential updates 相同的 center / readiness sequence。
15. Strategy code 不得存在任何可以呼叫 venue order API 的路徑。
16. 現有 Engine execution、emergency hedge、reconcile、recorder、reference 與 shutdown tests 必須全部繼續通過。

## 17. Acceptance criteria（驗收條件）

子專案 A 在以下條件全部成立時視為成功：

- 使用者可在 `config.yaml` 明確指定 `stable_basis` 或 `drifting_basis`。
- `stable_basis` 在等價參數下保留現有 strategy behavior。
- `drifting_basis` 能以約 1 Hz 的 causal rolling median 產生 center，並有保守的 warm-up 與 discontinuity handling。
- Engine 仍然負責 execution 與 risk controls。
- Strategy Library 不包含任何 order / API dependency。
- 相同的 strategy objects 未來可由 offline replay 驅動，不需要 live credentials。
- Live bot 不包含自動市場診斷或自動策略選擇。
- 現有完整 test suite 與新增的 strategy / regression tests 全部通過。

## 18. 後續子專案

本子專案實作並驗證完成後：

- **子專案 B — Market Analyzer Core（市場分析器核心）：** 手動提供歷史資料、可選時間範圍、Market Profile（市場剖面）、對 `stable_basis` vs `drifting_basis` 做 causal / walk-forward replay（因果／走勢前推重播）、輸出 recommendation / `NO_TRADE` 與 config snippet；只提供建議，不修改 live config。
- **子專案 C — Regime Detector（市場狀態偵測器）：** 由人工啟動，在使用者指定的大時間範圍內做 changepoint / regime splitting（變化點／市場狀態切分）；每個偵測到的區段都交給同一套 Market Analyzer Core 分析。它永遠不控制 live bot。
