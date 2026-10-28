# SNDK × Lighter RH: asynchronous two-leg execution assessment

Baseline: `c8a5fb7768d70e633e8f684926142fa5ee606f36`. This report uses the published Run02 execution-quality summary; **no raw Run02 JSONL was available in the GitHub connector**. No new empirical asynchronous execution backtest is claimed.

## Fixed assumptions

- Research fee: 0 bps on both venues; no claims about account fee eligibility.
- Targets: $100 and $250 USD buy-leg notional.
- Signals: 15,338 (BUY Entropy: 14,937; SELL Entropy: 401).
- Pair-skew eligibility: 250 ms.
- No trading, execution, partial fills, fees, funding, or queue data.

## Verified historical observations (same-time paired VWAP)

| Target | 0ms VWAP edge P50/P90 (bps) | 0ms positive edge fraction of all signals | 100ms complete paired-state fraction | 100ms positive edge fraction of all signals | 500ms complete paired-state fraction | 500ms positive edge fraction |
|---|---|---|---|---|---|---|
| $100 | 1.269 / 3.467 | 86.76% | 91.80% | 78.48% | 47.14% | 34.40% |
| $250 | 1.188 / 3.310 | 85.07% | 91.80% | 76.97% | 47.14% | 33.57% |

250ms complete paired-state fraction: 4.95%. This anomalous non-monotone sequence reflects pair-skew eligibility at different observation times, **not** order execution or fill probabilities. The statistics above evaluate both legs at the same observed state and therefore cannot measure sequential hedging PnL.

## What a valid asynchronous two-leg replay must do

For each actual signal and direction, locate the first pair state causally available at signal time. Choose first-leg fill at an independently specified hypothetical submission/arrival timestamp, consume five-level ask/bid depth for the target size, and freeze its executed base quantity. For each second-leg latency hypothesis (0, 50, 100, 250, 500 ms), independently select the second venue's most recent quote **received no later than the assumed second-leg arrival timestamp**; do not substitute a future update or use a two-venue pair-skew check as if it were an execution success rate. Walk the correct side of the second venue's five-level book for exactly the first-leg base quantity. Simulate both leg orders (Entropy first; RH first), and BUY/SELL directions separately. Keep the first-leg price fixed while repricing the second leg; report net bps and USD on first-leg notional, unhedged exposure if the second depth is insufficient, and source age of both quotes.

A crucial distinction: the latest received quote at arrival represents only a **historical executable-price proxy**, not a fill; using a quote first received after arrival is look-ahead. Order receipt, matching queue, cancellations, acknowledgment timing and partial fills remain unobserved.

Report denominator across *all* eligible original candidate IDs, distribution (P10/P50/P90) of simulated capture, gross positive fraction, second-leg depth sufficiency, unhedged quantity and tail adverse move, censoring, and comparison with simultaneous execution. No averaging of leg P50 losses or of P50 quotes can recover joint per-signal PnL. Avoid overlapping-event double counting when translating signals into putative trades.

## Decision

Current published summaries establish positive **simultaneous** 0-fee historical VWAP edge, especially at $100–$250. They **do not establish positive non-synchronous capture**. Status: **NO-GO for claiming demonstrated asynchronous profitability**, while a record-only second-leg replay on the locally retained 12 Run02 JSONL files is the narrowest missing analysis. The local raw files must be executed/read where stored; not available via GitHub files. Do not collect new data or change live strategies for this step.
