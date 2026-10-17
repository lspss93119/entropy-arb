# P0-03：未知執行結果的人工復核與重啟門檻

本文件適用於雙腿送單、成交結果、訂單終態或持倉對帳不確定的情況。它描述目前 P0-03 的人工接管流程；不代表已授權合併、部署、恢復交易或自動補單。

## 目前實作的安全契約

### 送單前的 durable marker

每次 LIVE 雙腿執行在 `_execute_locked` 內先寫入：

```text
<absolute trades_csv>.<symbol>.<hedge_venue>.unresolved
```

寫入內容是 UTC epoch timestamp 與原因，例如：

```text
1792209665.199 PREPARED before both-leg send
```

`_persist_unknown` 會先建立父目錄，對 marker 執行 `write`、`flush`、檔案 `fsync`，再對父目錄 descriptor 執行 `fsync`。只有整個持久化流程成功後，才會進入 `_execute`；因此若 marker 或目錄同步失敗，程式會進入 HALT／unresolved 狀態，並記錄 `NO ORDERS SENT`，不呼叫任一交易所送單路徑。

Marker 本身是事故閂鎖，不是成交帳本，也不包含 execution ID。execution ID 在 marker 成功後、進入 `_execute` 時才產生，並寫入 trades CSV；因此不能只用 marker 判定有成交或沒有成交，也不能因 trades CSV 沒有 row 就推定交易所沒有收到訂單。

### Live restart gate

LIVE 啟動時先取得以同一 marker 路徑衍生的 `.unresolved.lock`，使用同機 `flock(LOCK_EX|LOCK_NB)` 防止同一路徑的第二個 LIVE 程序。接著在建立 venue、載入 market、初始化 signer 之前檢查 marker：只要 marker 存在，就以

```text
Unresolved execution requires manual review before LIVE restart: <marker path>
```

拒絕啟動。這個 gate 不會自動清除 marker，也不會自動 replay、補單或平倉。`--record-only` 不初始化交易 signer、不送單，且不取得 LIVE lock；它不應被當作 LIVE recovery。

P0-03 的 process-level acceptance 已在隔離的真實 OS subprocess、fake venue/fake signer 測試中通過：在 marker 已持久化後強制終止程序，marker 仍存在；使用相同執行設定重啟時，在 venue 建立、market 載入及 signer/order side effect 前 fail closed。

### 正常 settlement 與清除

即使兩腿 `_execute` 返回，marker 仍保留到 hedge/reconcile 完成且 net delta 在設定容忍度內。只有完整 settlement 成功時才會 unlink marker，並再次 `fsync` 父目錄；任何 hedge、reconcile、取消或檔案錯誤都保留 marker 並要求人工復核。

## 人工復核流程

遇到 `.unresolved`、`send-unknown`、程序被中止或任何未預期執行例外時，依序處理：

1. **保持停止新開倉。** 不要刪除、移動、截斷或覆寫 marker；不要用重啟測試是否「恢復」。保留原始檔案、mtime、權限與完整路徑。
2. **盤點程序與路徑。** 確認同一主機沒有另一個 launchd、screen、tmux、nohup 或手動 LIVE 程序；確認所有程序使用同一個絕對 `trades_csv`、symbol、hedge venue 與 config。若可能有另一台主機運行同一帳戶／市場，先完成跨主機協調。
3. **查詢兩個交易所的權威訂單終態。** 使用可取得的 exchange order ID、client ID、execution ID 及時間範圍查詢成交、部分成交、取消、拒絕或未知。Marker 通常只有 timestamp/reason，沒有 execution ID；缺少本地 trades CSV 也不是「沒有送單」的證據。
4. **查詢兩腿權威持倉。** 在訂單終態明確後，分別取得 Entropy 與 hedge venue 持倉、未決訂單及淨曝險。不要用單次、可能落後的 position snapshot 或舊本地 state 取代兩腿 reconciliation。
5. **處理殘餘曝險。** 若存在單邊成交或未對沖部位，只能依已批准的人工風險流程處理；每次操作後重新查兩腿訂單與持倉。未知訂單仍可能成交時，不得盲目重複送單或用補單掩蓋不確定性。
6. **留存證據。** 保存 UTC 時間、原始 venue 回覆、order/client/execution ID、兩腿成交數量與價格、持倉前後值、操作人及決策。證據不足時維持停止狀態。
7. **解除人工閂鎖。** 只有在所有相關訂單已達明確終態、兩腿持倉已對帳、淨曝險已處理、沒有其他可能送單程序，且操作人已完成審核後，才可先備份 marker，再由操作人移除它。這不是程式自動動作，也不表示交易安全性已由 marker 單獨證明。
8. **重啟後再次驗證。** 確認 marker 不再存在、lock/config/path 正確，LIVE 啟動完成 reconciliation，並重新查兩腿持倉、未決訂單、HALT 與 net。任何不一致都維持停止並重新進入人工復核。

人工查詢與人工處理必須使用真實交易所的權威資料；本機 log、trades CSV、舊 state 或程序退出碼只能作為輔助證據。跨主機未完成協調時，不得解除 marker 或恢復 LIVE。

## 已知限制與 Canary 前門檻

- `.unresolved` 是 incident latch，不是跨程序交易 ledger；它不記錄完整 order 或 execution identity。
- `flock` 只涵蓋同一主機、同一路徑且使用本實作的 LIVE 程序，不能取代跨主機協調，也不能阻止不使用此機制的舊版程序。
- 改變 `trades_csv`、symbol、hedge venue、工作目錄或主機，可能讓程序看不到原 marker；部署必須固定並審核絕對路徑。
- Marker 寫入前的 filesystem／程序故障會 fail closed 而不送單；本文件不把未知故障轉成自動 recovery，也不宣稱可自動恢復所有外部交易所時序。
- 檔案與父目錄 `fsync` 是本機 durability 控制，不是交易所訂單已落帳的證明；部署主機仍需驗證可寫、磁碟健康與實際重啟行為。
- Canary 前必須完成：現有 marker 的逐筆人工復核、同機及跨主機程序盤點、兩腿未決訂單／持倉 reconciliation、完整 P0-01～04 測試與明確的手動接管／回滾授權。
- Dashboard 的既有 Rich truncation failure 屬獨立 baseline 問題，不是 P0-03 recovery 證據，也不能被本文件取代。

本文件不授權 merge、deploy、LIVE resume、下單或刪除任何 unresolved marker。
