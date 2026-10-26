# SNDK × Lighter RH Phase 3 分段研究

本研究只讀取 45 個 checksum 通過的 completed runs，排除 active run、不完整 run 與 checksum 不一致檔案。沒有讀取或修改 Engine、交易設定、Recorder 或帳戶資料。所有 Edge 是行情觀測值，不是成交或 PnL。

## 結論摘要

- P1：Entropy source age 明顯高於 RH。這是同步／接收品質差異的觀察，不等同於單向網路延遲因果。
- P2：250 ms replay 與實際 signals 在保留 quote segment 內逐筆相符率為 100.00%（11583/11583）。100／500 ms 結果是 retained quote segment 的反事實 replay，不是完整 run 的無偏事件數。
- P3：Persistence 結果只檢查訊號後第一個不早於指定 horizon 的 pair observation；它不是連續每毫秒 persistence，也沒有把未來資料帶入訊號產生。
- 不存在足夠的完整、未淘汰 quote 來把 500 ms skew 或 persistence 結果提升為實盤可執行性證據。Run02 建議維持現有 250 ms skew 與原策略參數；若要做一項額外測試，優先做獨立 read-only 的 250 ms persistence 觀測，不接入 Engine。
## 資料範圍與保留率

- Completed runs: 45; excluded runs: 1.
- Quotes retained: 660,645 / 1,335,549 written (49.47% weighted). Retention is per writer and varies by run; it is not assumed equal to signals or decay.
- Signals retained: 23,657. Decay rows retained: 118,285; expected 5 horizons per signal is 118,285.
- P2 replay quote coverage is limited to each run's retained time segment. Replay never joins adjacent runs.
- The 45 completed-run manifests have no recorded source timestamp missing/invalid rows. Quote age distributions below still use only rows with parameter_valid=true and non-negative finite age.
- Right-censored and late observations remain separate. Missing decay rows and invalid snapshots are not converted to zero.
## P1 — Source Age × Edge Decay

### Source age summary

| venue | P50 ms | P90 ms | P95 ms | P99 ms | max ms | usable rows | invalid Rolling rows | source missing | source invalid |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| entropy | 282.381 | 390.5247 | 493.1278499999998 | 698.6532600000002 | 3207.275 | 126804 | 29562 | 0 | 0 |
| lighter-rh | 24.751 | 27.477 | 28.728 | 37.71628999999998 | 3684.66 | 411372 | 92907 | 0 | 0 |

The source age is received_wall_ns minus the explicit source timestamp. It is not validated one-way network latency. The age-to-edge comparison is correlational and is affected by market state, feed scheduling, pair skew, and recorder retention.

### Actual 250 ms candidate decay

| direction | horizon ms | signal count | timely observed | observable % | edge loss p50 bps | edge loss p90 bps | late | right-censored |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| sell_entropy | 50 | 4212 | 2317 | 55.009496676163344 | 0.0 | 0.0 | 1895 | 0 |
| sell_entropy | 250 | 4212 | 2195 | 52.11301044634378 | 0.0 | 0.6415972117252977 | 2017 | 0 |
| sell_entropy | 1000 | 4212 | 2399 | 56.95631528964862 | 0.16182656681573704 | 3.9057362332186654 | 1811 | 2 |
| buy_entropy | 50 | 19445 | 10236 | 52.64078169195166 | 0.0 | 0.08044728808664914 | 9208 | 1 |
| buy_entropy | 250 | 19445 | 10088 | 51.87966058112625 | 0.0 | 0.8743343136470294 | 9355 | 1 |
| buy_entropy | 1000 | 19445 | 11286 | 58.04062741064541 | 0.0 | 2.722581611164232 | 8153 | 5 |

Positive edge loss means the later executable edge is lower than the signal edge. Late rows are outside the recorder's 50 ms sampling tolerance and have no edge value. Correlation rows are in p1_age_correlations.csv; a positive correlation means older source age is associated with larger edge loss, not that age caused the loss.

At the 1000 ms horizon, Spearman age versus edge-loss correlations are:

| venue | direction | n | Spearman | Pearson |
|---|---|---:|---:|---:|
| entropy | sell_entropy | 2399 | 0.2000332431982099 | 0.12129724936335148 |
| entropy | buy_entropy | 11286 | 0.09929360672047266 | 0.06864925304082511 |
| lighter-rh | sell_entropy | 2399 | 0.04144996996198263 | 0.0013409591234020016 |
| lighter-rh | buy_entropy | 11286 | 0.08927069833064381 | 0.016860417110535954 |

The correlations are weak to modest and are not consistent at the shorter horizons. Entropy sell is the strongest positive 1000 ms association in this sample; RH associations are near zero. The result does not support a causal source-age filter.

## P2 — Pair Skew

| skew threshold ms | candidate events | retention vs 250 % | candidate edge P50 bps | same-direction interarrival P50 ms |
|---:|---:|---:|---:|---:|
| 100 | 8831 | 76.24104290770957 | 1.5365952284684958 | 603.326 |
| 250 | 11583 | 100.0 | 1.6157699143648507 | 575.0719999999999 |
| 500 | 6608 | 57.04912371579038 | 1.4573719109367556 | 1243.254 |

The 500 ms case cannot recover events that were never recorded after the original 250 ms gate. The replay uses retained quotes only. The 100 ms case can also change the active latch and therefore is not a simple row filter. The 250 ms exact-match check is a replay integrity check, not proof that retained quotes are complete.

Selected post-filter decay comparisons:

- 500 ms horizon: 100 ms: p90 loss 1.610776742483222, qualifying 16.283546597214357%; 250 ms: p90 loss 1.5354056967520346, qualifying 23.335923335923336%; 500 ms: p90 loss 1.937804590864345, qualifying 29.64588377723971%.
- 1000 ms horizon: 100 ms: p90 loss 2.717288789608086, qualifying 7.349111085947231%; 250 ms: p90 loss 2.5365185432829622, qualifying 12.811879478546146%; 500 ms: p90 loss 3.1537824451157714, qualifying 28.329297820823246%.

Detailed post-signal decay for every skew threshold and horizon is in p2_skew_decay.csv.

## P3 — Persistence

| persistence horizon ms | candidate events | timely observed | qualifying at horizon | retained % | edge loss P50 bps | edge loss P90 bps | right-censored |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 11583 | 4872 | 4276 | 36.91617024950358 | 0.0 | 0.08115960925514366 | 1 |
| 250 | 11583 | 5001 | 252 | 2.1756021756021755 | 0.0 | 0.5705901532460089 | 1 |
| 500 | 11583 | 6886 | 2703 | 23.335923335923336 | 0.0 | 1.5354056967520346 | 3 |

Qualifying at persistence horizon means the first timely observation at or after the horizon still had a valid Rolling snapshot, passed the selected 250 ms skew gate, and met the direction threshold. This is a forward-looking offline label used to evaluate waiting cost. It is not a live decision implementation and does not prove continuous persistence between observations.

## Per-run and time segments

- Per-run coverage and candidate replay counts: run_summary.csv.
- Four-hour UTC segment counts and age/edge summaries: time_segment_summary.csv.
- The replay is reset at every run boundary. The first 1 second of each retained quote segment is used as warm-up and excluded from candidate counts to avoid treating an unknown pre-segment latch state as a new signal.

## Evidence strength and limitations

- P1 source-age percentiles are descriptive and have large retained-row coverage, but quote retention is approximately half of written quotes overall and varies by run. No causal latency claim is justified.
- P1 actual signal/decay files are complete for the selected runs, but late and right-censored horizons reduce usable edge samples.
- P2/P3 are strongest as implementation-consistency and directional sensitivity checks within the retained quote segments. They are not full-run event-rate estimates because the running collector used max_files=2 and evicted earlier quote files.
- No result is a fill, realized capture, funding result, or account PnL. There is no basis here to optimize a production trading threshold.

## Run02 recommendation

Maintain the existing 250 ms pair-skew setting and all formal strategy parameters. The single additional experiment worth running is a separate read-only 250 ms persistence observation at the existing candidate threshold, with complete quote retention and no Engine integration. Do not treat this recommendation as authorization to change or start trading.

## Verification

- New offline analyzer tests: 2 passed.
- Quote-latency focused suite including the analyzer: 25 passed.
- Full pytest: 121 passed and 1 known baseline Rich Dashboard width/truncation failure, also present on the baseline branch.
- compileall and git diff --check: passed.
- No Engine, Rolling, Inventory Ladder, IOC, Recorder, or formal strategy files were changed.

## Artifacts

- statistics.json contains machine-readable selection, data-quality, P1, P2 and P3 results.
- source_age_summary.csv
- p1_edge_decay_by_age.csv
- p1_edge_decay_overall.csv
- p1_age_correlations.csv
- p2_skew_summary.csv
- p2_skew_decay.csv
- p3_persistence_summary.csv
- run_summary.csv
- time_segment_summary.csv
- p1_source_age_edge_decay.png
- p2_p3_thresholds.png
