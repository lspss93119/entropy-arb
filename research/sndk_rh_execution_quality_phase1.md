# SNDK × Lighter RH Execution Quality Phase 1

本報告只使用 Run02 的完整 Quote、Signal、Decay 與五層訂單簿。VWAP、Edge、深度與延遲結果都是歷史行情模擬，不是實際成交、已實現 Capture 或 PnL。

## 資料與成本口徑

- Source commit: `46932e71adafd46852a366f88167fd8031fdead5`; market: `io:SNDK|lighter-rh:SNDK`。
- Frozen input: `/private/tmp/entropy-arb-run02-analysis-20261027/raw`; eligible completed Runs: 12/12。
- Quotes 628,027; Signals 15,338; Decay 76,690; Quote retention 100.0%。
- SHA mismatch 0; parse errors 0; drops 0; writer errors 0。
- $100/$250/$500/$1,000 定義為買入腿的目標 USD 名目。先走買入腿五層 ask 到目標，再要求賣出腿以相同 base quantity 有足夠五層 bid；不足即標記不可完整成交，不使用第五檔外推。
- `research_0bps` 來自 Run02 research fee setting。`entropy_raw_4_5bps` 是已知的 Entropy raw fee 情境加 RH 0 bps；Run02 沒有成交或 fee ledger，故目前帳戶適用性與返佣均 NOT VERIFIED。
- Run02 沒有實際 fill、funding、返佣或 execution ledger；本分析只做五層可見深度與費率敏感度，不估計 funding、queue position、partial fill 或已實現 PnL。
- `sell_entropy` 方向為買 RH ask、賣 Entropy bid；`buy_entropy` 方向為買 Entropy ask、賣 RH bid。VWAP 先以買入腿目標名目走深度，再以相同 base quantity 走賣出腿。
- 延遲狀態是每個 signal 在同一 Run 內第一個 `event_wall_ns >= signal_wall_ns + delay` 的雙腿觀測；沒有後續觀測就標記 missing，不跨 Run 拼接。

## 0ms 深度與成本比較

| Target | candidates | state/skew eligible | 5L depth sufficient | full executable | VWAP Edge P50/P90 | conservative net P50 | positive net | 100ms | 250ms | 500ms |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| $100 | 15338 | 99.99% | 100.00% | 99.99% | 1.269/3.467 bps | -3.231 bps | 4.43% | 4.69% | 0.27% | 1.18% |
| $250 | 15338 | 99.99% | 100.00% | 99.99% | 1.188/3.310 bps | -3.312 bps | 3.83% | 4.10% | 0.22% | 1.02% |
| $500 | 15338 | 99.99% | 99.99% | 99.99% | 1.033/3.119 bps | -3.467 bps | 3.17% | 3.57% | 0.22% | 0.81% |
| $1,000 | 15338 | 99.99% | 99.82% | 99.81% | 0.773/2.807 bps | -3.727 bps | 2.53% | 2.82% | 0.16% | 0.68% |

## BUY / SELL（保守費用情境）

| Direction | Target | 0ms complete | 0ms positive net | 100ms positive | 250ms positive | 500ms positive | 0ms buy/sell slippage P50 |
|---|---:|---:|---:|---:|---:|---:|---:|
| sell_entropy | $100 | 99.75% | 25.44% | 19.45% | 1.00% | 4.99% | 0.000/0.000 bps |
| sell_entropy | $250 | 99.75% | 22.94% | 16.96% | 1.00% | 4.49% | 0.000/0.022 bps |
| sell_entropy | $500 | 99.75% | 20.95% | 15.46% | 1.00% | 3.74% | 0.000/0.477 bps |
| sell_entropy | $1,000 | 98.75% | 18.70% | 13.47% | 1.00% | 3.49% | 0.000/0.969 bps |
| buy_entropy | $100 | 100.00% | 3.87% | 4.29% | 0.25% | 1.08% | 0.000/0.000 bps |
| buy_entropy | $250 | 100.00% | 3.32% | 3.76% | 0.20% | 0.93% | 0.000/0.000 bps |
| buy_entropy | $500 | 99.99% | 2.69% | 3.25% | 0.19% | 0.73% | 0.091/0.000 bps |
| buy_entropy | $1,000 | 99.84% | 2.10% | 2.54% | 0.14% | 0.61% | 0.488/0.000 bps |

## 延遲與不可執行情況

`positive net` 的分母是全部原始候選，因而同時反映深度不足、過期 pair skew、Rolling 無效與等待後無 Quote 的機會損失。`delay_bbo_edge_loss` 是 signal BBO Edge 減去延遲後 BBO Edge；VWAP Edge 仍只在兩腿五層可完整走完時統計。

| Target | Delay | complete | stale skew | buy depth short | sell depth short | latency missing | delay BBO loss P50/P90 | VWAP-vs-BBO P50 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| $100 | 0ms | 15337 | 0 | 0 | 0 | 0 | 0.000/0.000 bps | -0.000 bps |
| $100 | 100ms | 14081 | 1254 | 0 | 0 | 1 | 0.000/0.563 bps | -0.000 bps |
| $100 | 250ms | 759 | 14576 | 0 | 0 | 1 | 0.000/1.188 bps | -0.000 bps |
| $100 | 500ms | 7230 | 8104 | 0 | 0 | 1 | 0.000/2.376 bps | -0.000 bps |
| $250 | 0ms | 15337 | 0 | 0 | 0 | 0 | 0.000/0.000 bps | -0.000 bps |
| $250 | 100ms | 14081 | 1254 | 0 | 0 | 1 | 0.000/0.563 bps | -0.000 bps |
| $250 | 250ms | 759 | 14576 | 0 | 0 | 1 | 0.000/1.188 bps | -0.000 bps |
| $250 | 500ms | 7230 | 8104 | 0 | 0 | 1 | 0.000/2.376 bps | -0.000 bps |
| $500 | 0ms | 15336 | 0 | 1 | 0 | 0 | 0.000/0.000 bps | -0.128 bps |
| $500 | 100ms | 14080 | 1254 | 1 | 0 | 1 | 0.000/0.563 bps | -0.134 bps |
| $500 | 250ms | 759 | 14576 | 0 | 0 | 1 | 0.000/1.188 bps | -0.144 bps |
| $500 | 500ms | 7229 | 8104 | 1 | 0 | 1 | 0.000/2.376 bps | -0.152 bps |
| $1,000 | 0ms | 15309 | 0 | 24 | 4 | 0 | 0.000/0.000 bps | -0.528 bps |
| $1,000 | 100ms | 14053 | 1254 | 24 | 4 | 1 | 0.000/0.563 bps | -0.533 bps |
| $1,000 | 250ms | 758 | 14576 | 1 | 0 | 1 | 0.000/1.188 bps | -0.555 bps |
| $1,000 | 500ms | 7214 | 8104 | 13 | 3 | 1 | 0.000/2.376 bps | -0.546 bps |

## Edge size buckets

逐 target、direction、delay、fee scenario 的分桶結果在 `edge_bucket_summary.csv`。分桶依 signal 時的 signed BBO Edge，另保留 negative 與 unavailable。

## 結論

1. 在 0 bps 研究情境，小額 0ms VWAP Net Edge 中位數仍為正；但套用 4.5 bps Entropy raw fee 情境後，四種名目在 0ms 的 Net Edge P50 均為負，正 Net Edge 僅約 2.5–4.4% 的全部候選。這不是已成交利潤證據。
2. $100–$250 的五層深度完整率約 99.99%，$1,000 約 99.81%；較大名目 VWAP edge 中位數下降，且深度短缺增多。小額的歷史可觀測性較好，但不代表可下單。
3. BUY／SELL 的費用依實際 Entropy venue leg 映射；SELL Entropy 時費用落在賣 Entropy leg，BUY Entropy 時落在買 Entropy leg。兩腿 slippage 另列，不能只用一腿的 BBO premium 排名。
4. 250ms 延遲後只有約 4.95% 候選仍通過 pair-skew/狀態條件；500ms 約 47.0%。延遲後 BBO Edge loss 的 P90 約 0.56/1.19/2.38 bps（100/250/500ms），且這是觀測行情衰退，不是成交延遲的 PnL。
5. 最終建議：**NO-GO 進入真實成交測試**。Run02 沒有實際 fills、execution timestamps、部分成交或帳戶 fee/返佣 ledger；需要另行授權的受控驗證，不能把歷史 VWAP 模擬當成已實現收益。

## 產物

- `execution_quality_summary.csv`：主統計，含 0/100/250/500ms、BUY/SELL、四種 target 與兩個 fee scenario。
- `execution_quality_events.csv.gz`：逐 Signal × target × delay 的 CSV 狀態、VWAP、深度、slippage 與 fee-adjusted edge（解壓後為完整事件表）。
- `edge_bucket_summary.csv`：0–2、2–5、5–10、10+ bps 及 negative/unavailable 分桶。
- `execution_quality_integrity.json`：Run02 integrity 摘要與分析設定。
- `execution_quality_summary.svg`：無外部依賴的比較圖。
