# Run02 Persistence Analysis Outputs

These files are derived offline from the frozen, checksum-verified Run02
snapshot. Raw Quote/Signal/Decay JSONL is intentionally not committed here.

- `integrity_summary.json`: eligible completed runs, row counts, retention,
  SHA-256 comparison, source timestamp/depth status and Rolling validity.
- `persistence_summary.csv`: all-direction, BUY and SELL aggregates for 0,
  100, 250 and 500 ms.
- `persistence_events.csv`: one row per signal and wait horizon. `edge_loss_bps`
  is `initial_edge_bps - horizon_edge_bps`; it is observed BBO edge loss, not
  realized PnL. `continuous_survived_observed` covers only pair observations
  actually present in the same Run. `right_censored=true` means the horizon
  could not be observed before that Run ended.
- `run_summary.csv` and `time_segment_summary.csv`: run/time grouping.
- `file_checksums.csv`: local SHA-256 and the remote snapshot SHA-256 for each
  retained Run file.
- `persistence_summary.svg`: dependency-free comparison chart.

The 0 ms row is the no-wait candidate baseline. A nonzero pair-observation gap
is not filled or treated as a failure. These outputs therefore do not establish
continuous millisecond-level validity, execution quality, or trading PnL.
