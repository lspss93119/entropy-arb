# Optional Legacy Rolling Strategy (SNDK × Lighter RH)

This strategy is **opt-in**. If `strategy` is omitted, `mode: fixed`
remains the unchanged Engine behavior using fixed `thresholds`.

## Configuration

Copy the existing `config.example.yaml` to a separate configuration file
(for example `config.sndk-rh-rolling.yaml`), then add:

```yaml
strategy:
  mode: rolling_legacy
  rolling_csv: logs/minutes-SNDK-lighter-rh.csv
  sell_quantile: 0.80
  buy_quantile: 0.50
```

The `thresholds` block may be removed in rolling mode. The legacy fixed
mode still requires all three fields exactly as before.

**Important:** The file above must be dedicated to **one pair only**:
`io:SNDK` versus `lighter-rh:SNDK`. Existing Recorder CSV files do not
carry a market ID. If a source export does include a `market_key` or `market`
column, the provider rejects rows for another pair; for the existing schema,
the program cannot independently verify pair provenance. Select and review
the file path before using the live mode. Do not point it to the shared
multi-market Recorder output.

If recording in the same trading process, configure `recorder.csv` to the
same dedicated pair file. It must contain the original recorder columns:
`minute_ts`, `premium_close_bps`, `sell_edge_max_bps`,
`buy_edge_max_bps`, and `samples`.

The historical 8h window has 480 completed one-minute rows. At each UTC
whole-hour boundary, the independent strategy calculates:

```text
M = round(P50(premium_close_bps), 1)
SellRoom = sell_edge_max_bps - M - (entropy_fee + hedge_fee)
BuyRoom  = buy_edge_max_bps  + M - (entropy_fee + hedge_fee)
U = max(round(P80(SellRoom) * 2) / 2, 1.0)
L = max(round(P50(BuyRoom)  * 2) / 2, 1.0)
```

The independent rolling provider owns its parameters; it does not mutate
the fixed config. Startup warmup, missing/incomplete samples, or invalid
windows block new strategy executions (no fallback to fixed thresholds).
The order planner, IOC execution, Inventory Ladder, reconciliation, and
Journal execution semantics are shared and unchanged.

**Not deployed:** This branch does not run any production code, alter
the Mac mini recorders, or send exchange orders. The additional
quote-age and restart/stop safety reviews are explicitly deferred by
the operator and are not claimed to be passed.

## Validation

```bash
python -m pytest tests/test_rolling_legacy_strategy.py tests/test_rolling_midline_band.py
python -m pytest
python -m compileall -q entropy_arb
git diff --check
```

Review the known baseline Rich terminal truncation test separately;
do not interpret it as a new strategy regression without comparison.


## Pre-live offline source check (no trading)

Use the dedicated rolling example `config.sndk-rh-rolling.example.yaml`.
The fixed `config.example.yaml` is unchanged. Neither file contains
credentials. The pilot example has per-leg cap $100, per-order cap $25,
but **do not start it to validate parameters**: the normal `main.py`
invocation without `--record-only` can send real orders.

Instead, from repository root run:

```bash
python tools/verify_rolling_sndk_rh.py \
  --csv logs/minutes-SNDK-lighter-rh.csv \
  --effective-at 2026-10-20T11:00:00Z
```

The command is read-only, does not instantiate Engine, and exits with 0 for
a valid 480-minute window, 2 for an invalid window. Omit
`--effective-at` to check the most recent UTC hour. Check `valid`,
`rows`, `coverage`, `reason` and the three parameters in the JSON output.
A historical exported CSV verifies calculation only, **not** that Mac mini
has a current, automatically updating dedicated source.

The existing multi-market Recorder processes must not be modified or
restarted merely to validate the strategy. Verify source provenance
manually for the old schema, which has no embedded market key.

This step does not test signed account queries, true IOC fills, quote-age,
recovery, or authorize live trading.
