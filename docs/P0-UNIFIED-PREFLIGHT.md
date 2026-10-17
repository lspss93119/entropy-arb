# P0 最終部署前檢查（Draft PR #9）

## 已完成：隔離整合分支的離線證據
- P0-01 雙腿深度名義金額上限；P0-02 行情 age gate（預設 0）；P0-03 未知成交及送單前持久標記；P0-04 成交／對沖紀錄。
- CI 2026-10-17：[`38998479248`](https://github.com/lspss93119/entropy-arb/actions/runs/38998479248) **81 passed**；包含 await 中途取消、hedge / reconcile 交錯、新開倉 gate、CSV 失敗、標記生命週期、同機重複 Live lock。
- `Engine.run()` 在 Live 初始化前取得 `fcntl.flock(LOCK_EX|LOCK_NB)`，直到 shutdown 完成才釋放。唯讀模式不取得 lock。鎖檔路徑 `<resolved_trades_csv>.<symbol>.<hedge_venue>.unresolved.lock`，不在程序執行中刪除。

## 實際部署前仍必須由操作人確認
1. 所有 Live 進程使用**同一個絕對工作目錄與同一份 config**；`logging.trades_csv` 建議指定絕對路徑。禁止切換 cwd／設定新 `trades_csv` 路徑繞過既有標記。換機時也必須人工核對舊主機可能尚在運作。
2. 查驗同一交易對是否已有其他程序，包括 launchd / screen / nohup / systemd；`flock` 只能防止**同一主機且同一路徑**的並行進程，不能取代跨主機協調；也無法阻止不使用此機制的舊版 Bot。
3. 啟動前在兩間交易所檢查未決訂單、兩腿實際倉位及殘餘淨曝險。遇到 `.unresolved` 不得直接刪除以嘗試開機；須依 `docs/P0-03-MANUAL-RECOVERY.md` 人工確認訂單終態、倉位一致性。
4. 確認持久化目錄可寫、`fsync` 正常，且主機磁碟健康。MacBook Air 已量測本機 marker p99=0.1594ms（1000 次），**不是預計部署機器**的保證。
5. P0-02 `max_quote_age_sec=0` 預設關閉；已於週末唯讀量測 RH SNDK，但沒有美股正常開盤的完整兩腿同步樣本。不得將非交易時段 0% sampled block 誤解為實際失敗率。
6. 事前確認可停機、人工接管、原版回滾、保留 `.unresolved` 標記與交易所檢核證據；不要把 rollback 當成自動清倉。
7. 合併、部署、Canary/實盤各需獨立明確批准。**本文件不代表已完成生產環境驗收。**

## 審查判定
- **離線整合功能與安全回歸：PASS**
- **同機同路徑雙程序防護：離線 PASS**
- **實際部署路徑／跨主機併發／啟動前真實持倉：NOT VERIFIED**
- **可以直接實盤：NO**
- **是否可以開始正式合併審查：YES，仍須 PR 最終審閱與用戶批准**
