# Optional Legacy Rolling Strategy (SNDK × Lighter RH)

This strategy is **opt-in**. If `strategy` is omitted, `mode: fixed`
remains the unchanged Engine behavior using fixed `thresholds`.

## Configuration

Copy the existing `config.example.yaml` to a separate configuration file
(for example `config.sndk-rh-rolling.yaml`), then add:

```yaml
strategy:
  mode: rolling_legacy
  rolling_csv: logs/sndk-rh-minutes.csv
  sell_quantile: 0.80
  buy_quantile: 0.50
```

The `thresholds` block may be removed in rolling mode. The legacy fixed
mode still requires all three fields exactly as before.

**Important:** The file above must be dedicated to **one pair only**:
`io:SNDK` versus `lighter-rh:SNDK`. Existing Recorder CSV files do not
carry a market ID. Therefore the program cannot independently verify pair
provenance; select and review the file path before using the live mode. Do
not point it to the shared multi-market Recorder output.

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
