# Phase 2 — Run02 非同步雙腿行情回放

研究工具：`tools/analyze_sndk_rh_async_execution.py`；測試：`tests/test_sndk_rh_async_execution.py`。

## Mac mini / Codex 一次執行

1. 只在研究分支 `research/sndk-rh-quote-latency-v1` 拉取最新提交。檢查工作樹，勿覆蓋本機改動。
2. 驗證程式：`python -m pytest -q tests/test_sndk_rh_async_execution.py`、`python -m compileall -q tools/analyze_sndk_rh_async_execution.py`。
3. 依照既有 Run02 persistence / execution-quality 的 frozen dataset 輸入規格，確定 `load_dataset` 驗證所有 manifest / hash / retention。優先使用原先已驗證的 frozen snapshot；必要時從 Mac mini 本地 Run02 完整原始資料複製，不修改原檔。
4. 執行：
```bash
python -m tools.analyze_sndk_rh_async_execution \
  --data-root '/private/tmp/entropy-arb-run02-analysis-20261027/raw' \
  --output-dir '/Users/lspss93119/entropy-arb-quote-latency-research/phase2-async-analysis'
```
若 frozen snapshot 不存在，改用已通過 `load_dataset` 完整性驗證的 Run02 路徑，不可跳過完整性驗證。
5. 驗證 `async_events.csv` 行數、唯一 (run_id, signal_id, direction, target_usd, first_venue, delay_ms)、方向與分母、完整性與 zero-fee 運算；檢查同一候選在五種延遲下可能彼此相關，不將候選數當獨立交易筆數。
6. 製作一份研究結論：以 $100 / $250、0/50/100/250/500ms、Entropy 先成交 / RH 先成交、BUY / SELL 各自列出 complete、positive capture / all candidates、Capture P10/P50/P90、未對沖風險與尾部虧損。指出能否支持小額受控測試，給 GO/NO-GO/INCONCLUSIVE。
7. 提交必要研究報告、摘要 CSV、統計 JSON 與可審查的小型測試；大型事件表與原始資料保留於 Mac mini，不要提交原始 Quote，別修改正式 Engine 或正在運行的 Recorder。

## 重要限制

- 0 bps 是**使用者指定的研究假設**。
- 本工具用收到時刻以前的最新五層 Quote 作價格代理，假設第一腿以可見深度全部成交、第二腿延遲後全部成交；它不是實際成交回報或 PnL。
- `max_age_ms=1000` 是敏感度參數，不是由真實訂單往返延遲校準。
- 避免將 pair skew 在不同指定延遲下非單調的通過率誤稱為成交成功率。
- **請 Codex 一次完成驗證、分析、審查、提交與最終結論，不需要中途等待確認**。任何真實交易或正式策略修改都不在授權範圍。
