# SNDK × Lighter RH Run02 Persistence 唯讀實驗準備

## 結論

Run02 的 12 小時收集尚未啟動。60 秒 isolated record-only Smoke Test 已通過；資料寫入、Rolling snapshot、來源時間、BBO/五層深度、JSONL 完整性及 raw-data preservation 均符合啟動前條件。這份報告不代表交易、成交或 PnL 驗證。

研究 branch baseline／source：`ae7128137db2516e256386761b8bcbcdb4e240cf`。

## 目前 Mac mini 狀態

- Phase 3 Supervisor：`COMPLETED`, 48/48 runs, 0 failed；沒有停止或重啟。
- 既有分鐘 Recorder：兩個 `--record-only` process 仍在運作，PID 51001（Core 多市場）及 PID 51004（RH OAI/ANTH/SNDK）。
- Phase 3 既有目錄未被修改：`/Users/lspss93119/entropy-arb-quote-latency-research/20261024T040419Z/`。
- Run02 目錄：`/Users/lspss93119/entropy-arb-quote-latency-research/run02-20261026T115135Z/`。

## Run02 有效設定

設定檔已建立並以隔離目錄保存：

`/Users/lspss93119/entropy-arb-quote-latency-research/run02-20261026T115135Z/supervisor.json`

| 項目 | 值 |
|---|---|
| market | `io:SNDK|lighter-rh:SNDK` |
| source commit | `ae7128137db2516e256386761b8bcbcdb4e240cf` |
| rolling CSV | `/Users/lspss93119/entropy-arb-multi-market-recorder/logs/minutes-SNDK-lighter-rh.csv` |
| duration | 12 × 3600 秒 |
| rolling | P80/P50, fees 0 bps |
| depth | 5 levels |
| pair max skew | 250 ms |
| max file rows / bytes | 50,000 / 8,388,608 |
| max files | 4,096 per writer prefix |
| retention setting | 168 h post-run policy |
| raw preservation | `true` |
| disk cap | 4 GiB |

`preserve_raw=true` 是本研究 Recorder 的 opt-in 选项：保留檔案輪替，但停用 writer 的 retention/max-files eviction，也停用 CLI 的跨-run cleanup。預設值仍為 `false`，不改變既有 Recorder 行為。Supervisor 不載入 Engine、`.env`、私鑰或交易帳戶 API。

## 啟動前資料與容量核對

- RH 專用分鐘 CSV：`/Users/lspss93119/entropy-arb-multi-market-recorder/logs/minutes-SNDK-lighter-rh.csv`
- 最新 CSV snapshot：13,241 rows；`2026-10-17T07:11:00Z` 至 `2026-10-26T11:52:00Z`；0 duplicate、1 個歷史缺口（已知 `2026-10-25T09:33:00Z`）。
- 最新完整 rolling effective hour：`2026-10-26T11:00:00Z`，480/480、coverage 1.0、valid。
- 最新參數：`M=2.8`、`U=2.5`、`L=1.5` bps；SELL threshold `5.3` bps；BUY edge threshold `-1.3` bps。
- Mac mini filesystem 可用約 113 GiB。
- 既有 48 個 completed hourly runs：最大單 run 約 31.7 MB，平均約 14.5 MB；以最大實測速率外推 12 小時約 380 MB，低於 4 GiB。此為容量敏感度估算，不是對市場更新速率的保證。
- 既有 run 曾出現 `files_expired`，因為它們使用舊的 `max_files=2`。Run02 使用新隔離目錄及 `preserve_raw=true`，不會承接或清理 Phase 3 檔案。

## 60 秒 Smoke Test

執行時間（UTC）：`2026-10-26T11:53:16.740022Z` 至 `2026-10-26T11:54:17.106263Z`。

Run：

`/Users/lspss93119/entropy-arb-quote-latency-research/run02-20261026T115135Z/smoke-runs/20261026T115316739725Z/`

| 指標 | 結果 |
|---|---:|
| quote rows | 1,130 |
| signal rows | 5（BUY 4、SELL 1） |
| decay rows | 25（5 horizons × 5 signals） |
| lifecycle rows | 4 |
| source timestamp present/missing/invalid | 1,130 / 0 / 0 |
| BBO depth | 1,130/1,130 均有 5 bids + 5 asks |
| received timestamp order violations | 0 |
| dropped updates/lifecycle | 0 / 0 |
| writer errors | 0 |
| files expired | 0 |
| rotations | 0 |
| raw frame fields/lines | 0 |
| JSONL parse | 全部通過 |

Manifest `writer_stats.rows_written` 與實際 JSONL 行數完全一致：quotes 1,130、signals 5、decay 25、lifecycle 4。所有檔案均為單一未淘汰檔案；四個 smoke 檔案總計 953,694 bytes。

Smoke 的 5 個候選均使用 valid Rolling snapshot：`effective_at=2026-10-26T11:00:00Z`、coverage 1.0、`M=2.8/U=2.5/L=1.5`。起始 executable edge 為 SELL `5.625589` bps；BUY 分別為 `2.139800`、`-1.267327`、`-1.108385`、`-0.554192` bps。Decay status 為 observed 23、`late_quote_outside_tolerance` 2；沒有把 late observation 當成準時有效樣本。

## 原始資料保存與停止方式

12 小時 supervisor 尚未啟動，沒有目前 Run02 PID。經審查後若獲得獨立授權，才可使用：

```bash
PYTHONPATH=/Users/lspss93119/entropy-arb-quote-latency-research/run02-20261026T115135Z/app \
/Users/lspss93119/entropy-arb-multi-market-recorder/.venv/bin/python \
/Users/lspss93119/entropy-arb-quote-latency-research/run02-20261026T115135Z/app/tools/quote_latency_supervisor.py \
--config /Users/lspss93119/entropy-arb-quote-latency-research/run02-20261026T115135Z/supervisor.json
```

停止方式（僅供後續受控操作，不是本次執行）：對 supervisor PID 發送 `SIGTERM`，等待其將當前 child 收尾並寫出 manifest；不得直接刪除 runs。完成後以每個 run manifest、JSONL 行數與 SHA-256 重新核對。

## 測試與範圍

- 新增 raw-preservation tests：11 passed。
- Full pytest：124 passed、1 個既有 `tests/test_dashboard.py::test_renders_key_numbers` Rich 寬度截斷失敗；本次沒有修改 Dashboard。
- `compileall entropy_arb tools tests`：passed。
- `git diff --check`：passed。
- 修改僅限研究 Recorder／Supervisor、測試、Run02 config 與本報告；沒有修改 Engine、Rolling 策略公式、Inventory Ladder、IOC、正式設定或分鐘 Recorder。

## 狀態

**READY FOR REVIEW；12 小時收集尚未啟動。**

Smoke Test 證明資料管線可運作，不足以估計 100/250/500ms persistence 分布。正式 Run02 仍需審查後另行授權啟動。

本機保存的 machine-readable smoke evidence：

- `research/sndk_rh_run02_persistence_smoke_manifest.json`
- `research/sndk_rh_run02_persistence_smoke_validation.json`
- `research/sndk_rh_run02_persistence_smoke_result.log` (含 collector INFO log 與結尾 JSON)
