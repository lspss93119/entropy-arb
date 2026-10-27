# Run02 execution-quality replay

This directory contains an offline replay of the frozen, checksum-verified
Run02 snapshot for `io:SNDK × lighter-rh:SNDK`. It does not contain credentials,
orders, fills, or live venue calls.

## Method

- Each event is one recorded signal × target buy-leg notional × causal delay.
- Direction `sell_entropy` buys RH asks and sells Entropy bids. Direction
  `buy_entropy` buys Entropy asks and sells RH bids.
- The buy leg walks recorded levels until the target USD notional is reached;
  the sell leg walks the same base quantity. Any shortfall is incomplete; no
  price is invented beyond recorded depth.
- The 0/100/250/500 ms state is the first paired observation in the same Run
  whose local event receive time is at or after signal time plus the delay.
  Missing horizons remain unavailable and are never joined across Runs.
- `research_0bps` is the Run02 rolling research setting. The
  `entropy_raw_4_5bps` scenario applies 4.5 bps to the Entropy leg and 0 bps
  to RH as a sensitivity case derived from prior raw-fee context. Run02 has
  no account fee or rebate ledger, so current applicability is not verified.
- `execution_quality_summary.csv` distinguishes fully executable events from
  the subset with valid state/skew for which the five recorded levels were
  sufficient. `edge_bucket_summary.csv` buckets by the signal-time signed BBO
  edge. All edge values are historical observations, not realized PnL.
- The full event table is retained as `execution_quality_events.csv.gz` for
  repository delivery; decompressing it reproduces the local CSV byte-for-byte.

## Input integrity

See `execution_quality_integrity.json` for the eligible Run count, retained
row counts, source checksums, parse errors, and analysis settings.
