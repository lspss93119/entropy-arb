# SNDK × Lighter RH Run02 Persistence 唯讀分析

本報告只分析已完成且 checksum 驗證通過的 Run02 原始 Quote。所有 Edge 是行情觀測值，不是成交、Capture 或 PnL。未修改 Engine、策略參數、IOC、Recorder 或原始資料。

## 完整性

- Frozen input root: `/private/tmp/entropy-arb-run02-analysis-20261027/raw`; market: `io:SNDK|lighter-rh:SNDK`; recorder source commit: `46932e71adafd46852a366f88167fd8031fdead5`。
- Supervisor status: `COMPLETED`, 12/12 completed, failed `0`; eligible runs `12/12`。
- Snapshot files: 119; bytes: 578779750; SHA-256 mismatches: 0; JSON parse errors: 0。
- Quote rows: 628027/628027 retained (100.0000%). Signals: 15338; Decay: 76690。
- Source timestamps: `{'present': 628027}`；depth shapes: `{'5x5': 628027}`；Rolling valid 480/480: 626927, invalid: 1100。
- Drops: updates 0, lifecycle 0; writer errors 0; files expired 0。
- 12 個 Run 未跨界拼接；所有 Run `preserve_raw=true`。
- Rolling invalid rows by Run: 20261026T134354427778Z=8 (invalid:data_gap_or_insufficient_window=8); 20261026T144355933641Z=17 (invalid:data_gap_or_insufficient_window=17); 20261026T154356779042Z=8 (invalid:data_gap_or_insufficient_window=8); 20261026T164357349234Z=4 (invalid:data_gap_or_insufficient_window=4); 20261026T174358565078Z=3 (invalid:data_gap_or_insufficient_window=3); 20261026T184359440078Z=1006 (invalid:data_gap_or_insufficient_window=1006); 20261026T194400579380Z=17 (invalid:data_gap_or_insufficient_window=17); 20261026T204401927331Z=2 (invalid:data_gap_or_insufficient_window=2); 20261026T214404025676Z=10 (invalid:data_gap_or_insufficient_window=10); 20261026T224404993045Z=12 (invalid:data_gap_or_insufficient_window=12); 20261026T234406360245Z=8 (invalid:data_gap_or_insufficient_window=8); 20261027T004408519907Z=5 (invalid:data_gap_or_insufficient_window=5)。
- Invalid Rolling rows are retained as evidence and excluded from valid-parameter claims; the analyzer does not fill or downgrade the 480/480 requirement。

## Run 覆蓋

| Run | UTC start | UTC end | quotes | signals | pair observations | pair gap P50/P95/max (ms) |
|---|---|---|---:|---:|---:|---:|
| 20261026T134354427778Z | 2026-10-26T13:43:54.785283+00:00 | 2026-10-26T14:43:54.993332+00:00 | 73957 | 3281 | 73956 | 48.8460/83.1623/498.2560 |
| 20261026T144355933641Z | 2026-10-26T14:43:56.257150+00:00 | 2026-10-26T15:43:56.243755+00:00 | 69997 | 4445 | 69996 | 49.3750/100.3928/502.5720 |
| 20261026T154356779042Z | 2026-10-26T15:43:57.904709+00:00 | 2026-10-26T16:43:57.061768+00:00 | 62130 | 4347 | 62129 | 50.0240/134.7849/511.2650 |
| 20261026T164357349234Z | 2026-10-26T16:43:58.393303+00:00 | 2026-10-26T17:43:58.227857+00:00 | 60242 | 1938 | 60241 | 50.1595/146.6782/507.9170 |
| 20261026T174358565078Z | 2026-10-26T17:43:58.869246+00:00 | 2026-10-26T18:43:58.684757+00:00 | 56797 | 516 | 56796 | 50.4090/151.7933/612.4060 |
| 20261026T184359440078Z | 2026-10-26T18:43:59.798596+00:00 | 2026-10-26T19:43:59.617430+00:00 | 55135 | 241 | 55134 | 50.5390/153.9148/606.8940 |
| 20261026T194400579380Z | 2026-10-26T19:44:00.906485+00:00 | 2026-10-26T20:44:00.748260+00:00 | 45093 | 98 | 45092 | 51.2450/246.2880/960.6930 |
| 20261026T204401927331Z | 2026-10-26T20:44:03.039116+00:00 | 2026-10-26T21:44:03.011805+00:00 | 26410 | 3 | 26409 | 73.8940/451.0862/888.9160 |
| 20261026T214404025676Z | 2026-10-26T21:44:04.333252+00:00 | 2026-10-26T22:44:04.232600+00:00 | 31056 | 9 | 31055 | 54.5860/390.3562/887.4780 |
| 20261026T224404993045Z | 2026-10-26T22:44:05.497798+00:00 | 2026-10-26T23:44:05.493695+00:00 | 39291 | 48 | 39290 | 52.2340/295.4220/976.5330 |
| 20261026T234406360245Z | 2026-10-26T23:44:06.860575+00:00 | 2026-10-27T00:44:06.589570+00:00 | 54961 | 245 | 54960 | 50.5370/156.2362/669.0800 |
| 20261027T004408519907Z | 2026-10-27T00:44:08.943604+00:00 | 2026-10-27T01:44:09.070406+00:00 | 52958 | 167 | 52957 | 50.7955/172.0627/701.3220 |

## Persistence 定義

每個 Signal 只使用同一 Run 內、訊號之後首次到達指定等待時間的 pair observation。`sampled_continuous_survived` 表示該區間內所有已觀測 pair state 都仍符合方向門檻；靜默期間沒有被當成失效，並另列最大 pair observation gap。因而這是觀測事件連續性，不是對每一毫秒市場狀態的證明。沒有後續 pair observation 的事件列為 right-censored。

## 全方向比較

| wait | raw candidates | complete observed | right-censored | continuous survival | signal retention at horizon | first failure | recovery after failure | continuous valid P50 ms | edge loss P50 | edge loss P90 | remaining edge P50 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0ms | 15338 | 15338 | 0 | 100.0000% | 100.0000% | 0.0000% | N/A% | 0.0000 | 0.0000 | 0.0000 | 1.3467 |
| 100ms | 15338 | 15337 | 1 | 85.4404% | 85.8847% | 14.5596% | 3.3587% | 127.8830 | 0.0000 | 0.5631 | 1.4189 |
| 250ms | 15338 | 15337 | 1 | 2.5233% | 3.5533% | 97.4767% | 2.0401% | 272.9540 | 0.0000 | 1.1884 | 1.4227 |
| 500ms | 15338 | 15337 | 1 | 0.3064% | 33.4985% | 99.6936% | 36.8999% | 273.7770 | 0.0000 | 2.3757 | 1.1957 |

## BUY / SELL

完整方向別數據在 `persistence_summary.csv`；每個 Signal 的首次失效、恢復、右設限與觀測 gap 在 `persistence_events.csv`。`signal retention` 是等待時點仍符合門檻的比例，不等同於連續存活。

### sell_entropy

| wait | raw | complete | right-censored | continuous survival | signal retention | first failure | recovery | continuous valid P50 ms | edge loss P90 | remaining edge P50 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0ms | 401 | 401 | 0 | 100.0000% | 100.0000% | 0.0000% | N/A% | 0.0000 | 0.0000 | 2.1808 |
| 100ms | 401 | 401 | 0 | 64.5885% | 65.3367% | 35.4115% | 2.1127% | 118.4970 | 1.3656 | 2.3725 |
| 250ms | 401 | 401 | 0 | 1.7456% | 4.2394% | 98.2544% | 3.8071% | 245.5330 | 2.2699 | 2.3628 |
| 500ms | 401 | 401 | 0 | 0.2494% | 13.9651% | 99.7506% | 19.2500% | 245.5330 | 6.1636 | 1.2511 |

### buy_entropy

| wait | raw | complete | right-censored | continuous survival | signal retention | first failure | recovery | continuous valid P50 ms | edge loss P90 | remaining edge P50 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0ms | 14937 | 14937 | 0 | 100.0000% | 100.0000% | 0.0000% | N/A% | 0.0000 | 0.0000 | 1.3395 |
| 100ms | 14937 | 14936 | 1 | 86.0003% | 86.4364% | 13.9997% | 3.4433% | 128.0605 | 0.5559 | 1.3554 |
| 250ms | 14937 | 14936 | 1 | 2.5442% | 3.5348% | 97.4558% | 1.9923% | 273.4385 | 1.1794 | 1.4177 |
| 500ms | 14937 | 14936 | 1 | 0.3080% | 34.0229% | 99.6920% | 37.3741% | 274.1735 | 2.3057 | 1.1948 |

## 結論與證據強度

Persistence 的等待結果不是單調改善：100ms 的全方向 sampled continuous survival 為 85.44%、horizon retention 為 85.88%；250ms 分別為 2.52% 與 3.55%；500ms 分別為 0.31% 與 33.50%。500ms 的 horizon retention 高於 250ms，表示部分事件在中途失效後重新同步／恢復，不能把 horizon 通過率解讀成連續 Persistence 品質。
BUY／SELL 方向不同：100ms horizon retention 為 BUY 86.44%、SELL 65.34%；250ms 為 BUY 3.53%、SELL 4.24%；500ms 為 BUY 34.02%、SELL 13.97%。這支持分方向描述，但不支持在正式策略中直接採用任一等待門檻。
Edge Loss 使用 signal edge - horizon edge 定義；它與 remaining edge 都只是可觀測 BBO Edge。資料中的 pair observation gap P95 約 110–226ms（依等待窗口變化），所以不能證明 gap 內每個瞬間都持續有效；也沒有成交、queue、滑價或 PnL 證據。相關性不代表因果關係。

目前唯一建議：**C，資料不足，暫不決策，也不把 Persistence 接入正式策略**。Run02 足以量化觀測事件的等待成本與恢復現象，但不足以證明哪個門檻改善實際執行品質；需要更細的同步觀測／成交證據後再另行審查。

## 產物

- `integrity_summary.json`：manifest、row、hash、retention 與品質驗證。
- `file_checksums.csv`：每個 Run 檔案的本地／遠端 SHA-256。
- `persistence_summary.csv`：全方向及 BUY/SELL 的四個等待條件。
- `persistence_events.csv`：逐 Signal 觀測結果。
- `run_summary.csv`、`time_segment_summary.csv`：逐 Run／時段分布。
- persistence_summary.svg
