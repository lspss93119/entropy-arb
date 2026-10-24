# SNDK × Lighter RH High-Frequency Recorder Validation

## Current result

The three requested research fixes were verified, the 60-second read-only smoke passed, and the bounded 48-hour collector is now running on Mac mini. This is a public-market-data process only; no credentials, account API, signing, orders, or trading Engine are involved.

## Version

- Repository: `lspss93119/entropy-arb`
- Branch: `research/sndk-rh-quote-latency-v1`
- Verified GitHub fix commit before supervisor addition: `3d0632e4ecb3abfe1a4f3b42868fe44600cd4841`
- Final branch HEAD after the supervisor/test/config addition: `7279ac7c7ffe9ae95eb3c5840ef7df26f063b338`
- Working tree: clean after push

The recorder behavior under validation is from `3d0632e...`; `7279ac7...` adds only the bounded long-run supervisor, its test, the non-secret config, and the 250ms/50ms boundary-test correction.

## Fix verification

1. **Late decay samples:** each 50/100/250/500/1000ms horizon records `sampling_lag_ms`. A sample is `observed` only when the first available quote is no more than 50ms after the target; later samples are `late_quote_outside_tolerance` and do not receive edge/parameter fields. Missing samples remain distinct.
2. **Bounded quote rows:** quote JSONL contains normalized timestamps, BBO, five-level depth, local clocks, connection ID, and Rolling fields; it does not contain `raw_frame` or an unbounded WebSocket payload.
3. **Cross-run cleanup:** only completed directories with `schema_version=1`, `record_only=true`, and the exact `io:SNDK|lighter-rh:SNDK` market are eligible for expiry. Active runs without a manifest, unrelated markets, symlinked folders/files, and unknown manifests are retained.

The latest test initially exposed a boundary error in its own fixture: a quote 50ms late is within a `<=50ms` tolerance. The fixture was corrected to 51ms late; production behavior was not changed for that boundary.

## Tests and static verification

- Focused quote-latency, recorder, and supervisor tests: **23 passed**.
- Full pytest: **119 passed, 1 failed**.
- The only failure is the known baseline Rich Dashboard truncation assertion: `tests/test_dashboard.py::test_renders_key_numbers`. The same failure existed on the base branch; it is outside this research change and was not modified.
- `PYTHONPYCACHEPREFIX=/private/tmp/entropy-arb-quote-latency-pyc-validation python3 -m compileall -q entropy_arb tools tests`: passed.
- `git diff --check`: passed.
- CLI and supervisor `--help`: passed.

No test or source path loads `.env`, private keys, or trading-account APIs. Engine, Fixed/Rolling decision logic, Inventory Ladder, IOC, and minute Recorder schema are unchanged.

## 60-second Mac mini smoke

The smoke used the existing minute source:

`/Users/lspss93119/entropy-arb-multi-market-recorder/logs/minutes-SNDK-lighter-rh.csv`

It ran from `2026-10-24T04:01:39.611689Z` to `2026-10-24T04:02:41.075877Z` in an isolated temporary directory. The source snapshot at the run was valid and used 0 bps research fees, sell P80, and buy P50.

| Metric | Result |
|---|---:|
| Quote rows | 528 |
| Entropy / HL rows | 112 |
| Lighter RH rows | 416 |
| Source timestamp present / missing / invalid | 528 / 0 / 0 |
| Source fields | HL `data.time`: 112; RH outer `timestamp`: 416 |
| Source age P50 / P95 / P99 | 26.6015 / 307.89075 / 454.75647 ms |
| Source age min / max | 23.995 / 611.579 ms |
| Depth | 5 bids and 5 asks on every row |
| Full raw frame rows | 0 |
| Lifecycle events | 4 |
| Reconnects | 0 |
| Quote/lifecycle drops | 0 / 0 |
| Writer errors | 0 |
| Rolling snapshot | valid, 480/480, coverage 1.0 |
| M / U / L | `-1.2 / 2.5 / 1.0` bps |
| Candidate signals / decay rows | 0 / 0 |

The age values are local wall-clock differences from source timestamps, not validated one-way network latency. No candidate signal occurred; therefore this smoke does not establish an empirical Edge Decay distribution.

The two existing minute Recorders remained unchanged and running with their original PIDs/commands (`51001` Core and `51004` RH). They were not restarted or modified.

## 48-hour collector

Independent Mac mini root:

`/Users/lspss93119/entropy-arb-quote-latency-research/20261024T040419Z/`

| Item | Value |
|---|---|
| Supervisor PID | `11653` |
| Current one-hour child PID | `11654` |
| State | `RUNNING` at validation time |
| Supervisor manifest | `.../supervisor-manifest.json` |
| Supervisor log | `.../supervisor.log` and `.../supervisor-launcher.log` |
| Run data | `.../runs/<UTC-run-id>/` |
| Target | 48 sequential runs × 3600 seconds |
| Retention | 72 hours |
| Data cap | 4 GiB under `runs/` |
| Per-prefix bound | 8 MiB/file, 2 files, 4 JSONL prefixes per run |
| Failure bound | stop after 3 consecutive failures; 300-second backoff between retries |
| Timeout | 3600-second capture plus 120-second grace |
| Source | current SNDK×RH minute CSV; Rolling P80/P50, 0 bps research fee |

The supervisor invokes the existing CLI once per hour with `--duration-sec 3600`; it does not bypass the CLI limit or run a tight restart loop. Each CLI invocation creates a unique UTC run directory and writes its own manifest. Its subprocess environment is sanitized to benign process settings only, and the supervisor does not read credentials.

At launch, `supervisor-manifest.json` reported `status=RUNNING`, `completed_runs=0`, `failed_runs=0`, and `current_child_pid=11654`.

To stop the collection in a controlled way, send `SIGTERM` to the supervisor PID:

```bash
ssh trading-mini 'kill -TERM 11653'
```

The supervisor then terminates the current child and records `status=STOPPED`; this command was documented but not executed during validation.

## GO / NO-GO

**GO for the requested bounded 48-hour read-only collection.** Focused tests, static checks, the latest source fixes, and the 60-second public-feed smoke passed. The existing full-suite Dashboard failure remains a documented baseline issue.

**NO-GO for any trading or performance conclusion.** The collection is not a trading Engine, and no candidate signal was observed in the smoke. Edge-decay statistics require the completed 48-hour dataset and must distinguish timely, late, and missing horizons.
