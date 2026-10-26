# SNDK × Lighter RH Phase 3 資料完整性審查

本報告是唯讀審查結果。未停止、重啟或延長 Supervisor／Recorder，未啟動交易，未修改交易策略、Rolling P80/P50、Inventory Ladder、IOC 或原始分鐘 CSV。

## 結論摘要

- 48 小時收集尚未完成：最新 Supervisor manifest 顯示 45/48 runs，failed_runs=0，Supervisor PID 11653 及 Child PID 27769 仍在執行。
- 本次沒有停止或重啟收集程序，也沒有自行補收或延長研究。
- 分鐘資料在 2026-10-25T09:33:00Z 缺少一筆。這造成 effective_at 10:00Z 至 17:00Z 共 8 個 8 小時窗口為 479/480；18:00Z 起窗口恢復 480/480。
- 目前證據支持「持續的單分鐘資料缺口」，不支持將它解釋為整點後短暫尚未 flush。Recorder process 自 2026-10-17 起仍存活，CSV 在後續快照中仍未出現該分鐘。
- 剩餘資料可支援分段的 Edge Decay／行情品質分析，但不能宣稱完整 48 小時資料集，也不能恢復已被跨 run retention 淘汰的歷史 quote。
- 資料完整性審查結論：NO-GO（針對完整 Phase 3 資料交付）；不代表交易策略或交易引擎的結論。

## 1. Git 與執行版本

| 項目 | 結果 |
|---|---|
| 研究分支 | research/sndk-rh-quote-latency-v1 |
| 驗證 HEAD | 767dd6512a5f9b57097271f4e997dcaae34dffc8 |
| 遠端 branch | 與上述 SHA 一致 |
| 研究程式 source_commit | 3d0632e4ecb3abfe1a4f3b42868fe44600cd4841 |
| 本機正式 checkout | 原有 feature/rolling-legacy-strategy-v1，保留既有未追蹤 research／docs/superpowers |
| 驗證方式 | 使用乾淨 isolated clone 驗證研究 branch；沒有覆蓋正式 checkout |

本機正式 checkout 的 Git metadata 不允許寫入 FETCH_HEAD，因此以 git ls-remote 驗證遠端 branch SHA，並在 /private/tmp/entropy-arb-phase3-repo-20261026 使用乾淨 clone 完成測試、提交及推送。

## 2. Supervisor、Child 與既有 Recorder

檢查時間約為 2026-10-26T01:50:19Z。

| 項目 | 結果 |
|---|---|
| Supervisor | PID 11653，RUNNING，PPID 1 |
| Current child | PID 27769，仍為 Supervisor child |
| Completed runs | 45 |
| Failed runs | 0 |
| Consecutive failures | 0 |
| Target runs | 48 |
| Last return code | 0 |
| Market | io:SNDK × lighter-rh:SNDK |
| Record-only | true |
| 最新 runs 目錄容量 | 約 669,736,777 bytes |
| 4 GiB 使用率 | 約 15.59% |
| 最新檔案寫入 | 約 2026-10-26T01:50Z，收集仍持續 |

目前兩個既有分鐘 Recorder 也仍在運作：

- PID 51001：Lighter Core，多市場 record-only。
- PID 51004：Lighter RH，OAI／ANTH／SNDK record-only。

本次沒有碰觸上述程序。

## 3. 唯讀備份與 checksum

備份目錄：

/private/tmp/entropy-arb-phase3-integrity-20261026.rXSEtP/

備份 manifest：

/private/tmp/entropy-arb-phase3-integrity-20261026.rXSEtP/backup_manifest.json

備份包含當時可讀取的 runs、Supervisor manifest／log，以及 SNDK × RH 分鐘 CSV snapshot。結果：

| 項目 | 數量／結果 |
|---|---|
| Remote files（hash snapshot） | 269 |
| Local files（manifest 寫入前） | 268 |
| Remote files 在 hash 期間穩定 | 269 |
| Checksum matches | 264 |
| Checksum mismatches | 5 |
| Local missing files | 1 |
| Completed runs | 45 |
| Active runs | 1，20261026T010644049112Z |

45 個已完成 runs 的 258 個檔案全部 checksum 相符。差異集中在仍在寫入的 active run，以及來源分鐘 CSV 在複製後繼續成長；active run 不被當作完整備份。沒有刪除或覆寫原始資料。

差異檔案均已列在 backup_manifest.json，包含 active run 的 decay／quotes／signals 檔案及 source/minutes-SNDK-lighter-rh.csv。這些差異是快照時間差，不是以猜測補齊。

### Quote retention 統計

以下為 45 個 completed runs 的可驗證統計；active run 沒有完成 manifest，因此其 written／evicted 不推估：

| 項目 | 結果 |
|---|---:|
| quotes_written | 1,335,549 |
| retained quote rows in backup | 674,581 |
| derived evicted rows | 674,904 |
| quote files expired | 67 |
| quote parse errors | 0 |
| active run quote rows retained | 13,936，2 個 quote files |
| active run written／evicted | NOT_AVAILABLE |

derived evicted rows 只是 completed-run manifest 的 written 減 retained 算術差額，不是已恢復的逐筆資料；已淘汰內容不可由本次備份重建。

目前實際執行中的 child command 使用 max_files=2。最新研究 branch 的 supervisor config 已有 max_files=32，並維持 max_file_bytes=8388608、max_data_bytes=4294967296（4 GiB）、retention_hours=72；這個新設定未追溯改變本次既有 run，也沒有在本次自行重啟套用。

## 4. 分鐘 CSV 與 479/480 缺口

遠端來源：

/Users/lspss93119/entropy-arb-multi-market-recorder/logs/minutes-SNDK-lighter-rh.csv

本次唯讀 snapshot：

/private/tmp/entropy-arb-phase3-integrity-20261026.rXSEtP/source/minutes-SNDK-lighter-rh.csv

snapshot 觀察到：

- 12,634 rows。
- 首筆 2026-10-17T07:11:00Z。
- 末筆 2026-10-26T01:45:00Z。
- Schema 包含 minute_ts、time_utc、Entropy／hedge bid／ask、premium OHLC、sell／buy edge、samples。
- 取樣當下來源仍在寫入；遠端 hash snapshot 為 5618365b7fbdf2c64d30dd738cd23bd0af8c3994e3e9db0759ec5c25dd248a3c。複製後來源繼續更新，因此不能把該 hash 宣稱為本地副本 hash。

使用 HEAD 767dd 的 tools/audit_sndk_rh_minute_gaps.py 對 2026-10-24T04:00Z 至 2026-10-26T01:00Z 共 46 個 effective hours 稽核：

| 類別 | 結果 |
|---|---:|
| Windows audited | 46 |
| Valid 480/480 | 38 |
| Invalid | 8 |
| Duplicate timestamps | 0 |
| Low-sample minutes below 10 | 0 |

8 個失效 effective hours 為：

2026-10-25T10:00Z、11:00Z、12:00Z、13:00Z、14:00Z、15:00Z、16:00Z、17:00Z。

每個窗口都是 rows=479，唯一缺少：

2026-10-25T09:33:00Z

鄰近資料：

| minute | samples |
|---|---:|
| 2026-10-25T09:32:00Z | 49 |
| 2026-10-25T09:33:00Z | missing |
| 2026-10-25T09:34:00Z | 39 |
| 2026-10-25T09:35:00Z | 60 |

09:33 不是低於 10 samples，而是整列不存在。後續 CSV snapshot 仍未補回該分鐘；18:00Z 窗口因為其 8 小時範圍已移出 09:33，恢復 480/480。2026-10-26T01:00Z 也為有效窗口。

Recorder process 沒有重啟或中斷證據。遠端 launchd-rh.stderr.log 在本地時鐘約 17:34 記錄過 Entropy connected、snapshot，以及隨後的 opening-handshake timeout/reconnect；該 log 缺少日期與明確 symbol，故只能作為可能的時間相關線索，不能證明根因。最嚴謹的結論是：目前觀察到 persistent single-minute feed／ingestion gap，並非已被證明的 Recorder flush race。

## 5. Rolling Provider 行為

目前實作以 CSV mtime_ns 與 size 組成 source signature：

- 同一 effective hour 若窗口有效，重複讀取不會不必要重算。
- 同一 effective hour 若窗口無效，只有 source signature 改變才重試。
- CSV 尾端在讀取中變動或窗口不完整時 fail closed。
- 不會因 invalid Rolling snapshot fallback 到 Fixed。
- 不會使用未完成分鐘或未來資料。

既有 regression test test_provider_does_not_use_incomplete_tail_and_retries_after_flush 已通過。

離線 verifier 使用本次 snapshot、fees=0.0 的結果：

| effective_at | rows | coverage | valid | reason / parameters |
|---|---:|---:|---|---|
| 2026-10-25T14:00Z | 479 | 0.9979167 | false | data_gap_or_insufficient_window |
| 2026-10-25T18:00Z | 480 | 1.0 | true | M=0.8, U=4.5, L=1.0 |
| 2026-10-26T01:00Z | 480 | 1.0 | true | M=3.6, U=2.5, L=1.0 |

這證明缺口會使 Rolling snapshot 無效，而不會製造交易訊號；它不代表該窗口的行情報價或 Edge 有足夠樣本可作完整回測。

## 6. 程式與測試驗證

在乾淨研究 branch clone 執行：

- quote-latency focused pytest：24 passed。
- full pytest：119 passed，1 failed。
- compileall entropy_arb tools tests：passed。
- git diff --check：passed。

唯一失敗是 tests/test_dashboard.py::test_renders_key_numbers，Rich 終端寬度截斷導致 sell_entropy 不在輸出。相同測試在 origin/main 的 baseline clone 也失敗，因此判定為既有 Dashboard 問題，不是本研究 branch 引入的回歸；本次沒有修改 Dashboard。

另外確認：

- research/sndk_rh_quote_latency_48h_supervisor.json 的 max_files=32 已存在。
- 4 GiB data limit 與 72 小時 retention 保持。
- cross-run cleanup 的 target 是研究資料自己的 run／manifest 範圍，不會以 backup 目錄為清理目標；本次備份位於獨立目錄。
- gap audit 能輸出 missing、duplicate、low-sample minutes。
- 本次沒有修改 Engine、Rolling P80/P50、Inventory Ladder、IOC 或交易設定。
- 研究 recorder 不載入私鑰、不使用交易帳戶 API。

## 7. 是否需要補收

目前不自行補收、不自行重啟，也不另開新的 Supervisor：

1. Supervisor 尚在進行且為 45/48，應讓現有收集自然完成，不中斷。
2. 已發現的缺口是資料本身的單分鐘缺失，不是可由降低 480/480 要求安全修復的資料。
3. 已淘汰的 674,904 quote rows 無法由現有檔案恢復；不可用假設數據補造。
4. 目前容量約 15.59% of 4 GiB，沒有容量迫使停止或清理的證據。
5. 如後續要補充缺口，應在另一次明確授權下使用獨立資料目錄與 max_files=32 設定；本次不執行。

因此，剩餘資料可以支援分段 Phase 3 分析，不能作為完整 48 小時資料交付。資料完整性狀態為 NO-GO；收集程序本身仍為 RUNNING。

## 8. 產物與證據位置

- 本報告：research/sndk_rh_quote_latency_integrity_review.md
- 聚合 JSON：research/sndk_rh_quote_latency_integrity_aggregate.json
- 唯讀備份 manifest：/private/tmp/entropy-arb-phase3-integrity-20261026.rXSEtP/backup_manifest.json
- 唯讀備份資料：/private/tmp/entropy-arb-phase3-integrity-20261026.rXSEtP/
- 研究 branch clone：/private/tmp/entropy-arb-phase3-repo-20261026/

所有時間均以 UTC 表示。缺失欄位或尚未完成的 active run 沒有被推估為完整資料；matched quote／signal／decay 統計只代表已實際保留和解析的 rows。
