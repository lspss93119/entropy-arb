# SNDK × Lighter RH Quote-Latency Research — Phase 1/2

## Outcome

Phase 1 timestamp audit and Phase 2 read-only high-frequency recorder are complete on the research branch. The recorder is opt-in, restricted to `SNDK × lighter-rh` (`io:SNDK` × market ID `32`), uses public WebSocket feeds only, and contains no order, account, credential, or Engine path.

This is market-data evidence only. The smoke run produced no orders, fills, PnL, or live-trading state.

## Version and scope

- Repository: `lspss93119/entropy-arb`
- Branch: `research/sndk-rh-quote-latency-v1`
- Starting/base SHA: `e7ae4d7cc48493de277ef7d3a2194e2238d26e7c`
- Implementation commits: `bc24df1`, `cc7ea2a`, `641c927`
- Existing minute Recorder: unchanged and not restarted
- `engine.py`, Rolling P80/P50, Fixed, Inventory Ladder, IOC, and price protection: unchanged

The only shared-file change is optional callback instrumentation in `entropy_arb/feeds.py`; with callbacks omitted, the existing feed behavior remains unchanged.

## Timestamp audit

| Venue | Observed frame | Source fields and unit | Local receive fields | Missing/invalid behavior |
|---|---|---|---|---|
| Entropy / Hyperliquid | `channel=l2Book`, `data.coin=io:SNDK`, `data.levels` | `data.time`, observed 13-digit epoch milliseconds | `received_wall_ns`, `received_mono_ns`, `connection_id` | `source_age_ms=null`; never converted to zero |
| Lighter RH | `channel=order_book:32`, `order_book.bids/asks`, `nonce`/`begin_nonce` | Outer `timestamp`, observed 13-digit epoch milliseconds; sample also contains `order_book.last_updated_at` as a 16-digit field and it is preserved in `raw_frame` | `received_wall_ns`, `received_mono_ns`, `connection_id`; snapshot/diff lifecycle | absent source field is `missing`; malformed field is `invalid`; age remains null |

The recorder computes source age only when a recognized explicit source field exists:

```text
source_age_ms = (received_wall_ns - source_timestamp_ns) / 1e6
```

The monotonic receive clock is retained for local ordering and interval analysis, but is not mixed with the exchange/source clock. A negative or unusually large source age is therefore evidence of clock/field semantics that needs separate treatment, not proof of zero or negative network latency. The RH `nonce` is sequence state, never a timestamp. The current parser uses the RH outer `timestamp` when present; `last_updated_at` remains in the raw frame because its authoritative meaning for latency attribution was not independently established.

### Raw packet samples from the final smoke

The following are reduced public samples; the stored quote rows retain the complete raw frame.

```json
{"channel":"l2Book","data":{"coin":"io:SNDK","time":1792813137075,"levels":[[{"px":"1241.1","sz":"2.3507","n":8}],[{"px":"1241.2","sz":"1.5304","n":13}]]}}
```

```json
{"channel":"order_book:32","timestamp":1792813137568,"order_book":{"last_updated_at":1792813136650493,"nonce":3888011880,"bids":[{"price":"1241.02","size":"0.4850"}],"asks":[{"price":"1241.20","size":"0.1610"}]}}
```

In the final smoke, the example computed source ages were approximately `477.457 ms` for the HL frame and `35.593 ms` for the RH frame. These are local wall-clock differences, not validated one-way network latency.

## Phase 2 recorder

Implemented files:

- `entropy_arb/quote_latency.py`: timestamp normalization, bounded depth snapshots, executable-edge formulas matching the existing minute Recorder, rolling parameter fields, signal latch, and 50/100/250/500/1000 ms first-observation decay tracking.
- `entropy_arb/quote_latency_recorder.py`: independent public-feed recorder. It accepts only `symbol=SNDK`, `hedge=lighter-rh`, and `rh_market_id=32`; it never imports or starts the trading Engine.
- `tools/record_sndk_rh_quote_latency.py`: explicit CLI with required pair selection, bounded output, retention and endpoint options. It creates a unique run directory and refuses to overwrite an existing manifest.
- `entropy_arb/feeds.py`: optional update/lifecycle callbacks, source-independent local wall/monotonic capture, connection IDs, and RH nonce-gap lifecycle events.

Each quote row records venue, message kind/channel, full raw public frame, BBO/depth (default five levels), source timestamp status/field/unit/raw/ns, source age, local wall/monotonic receive times, connection ID, and the current Rolling parameter snapshot. Candidate rows record the threshold and the same parameter fields. The candidate thresholds are `M + U` for `sell_entropy` and `L - M` for `buy_entropy`; this is an observation filter only and does not place or simulate orders.

Storage is bounded per JSONL prefix by rows, bytes, file count, and retention hours. The final smoke used `max_file_rows=1000`, `retention_hours=1`, and `max_files=4`. Rotation/retention is scoped to the recorder's own prefix and does not touch minute Recorder files.

## Mac mini read-only smoke

Run: `2026-10-24T03:38:57.290393Z` to `2026-10-24T03:39:07.453746Z` (10 seconds), isolated output under `/tmp/entropy-arb-quote-latency.hdtRmJ/out3/`.

| Measure | Result |
|---|---:|
| Quote rows | 111 |
| Entropy / HL rows | 19 |
| Lighter RH rows | 92 |
| Source timestamp present / missing / invalid | 111 / 0 / 0 |
| Source field counts | `data.time`: 19; `timestamp`: 92 |
| Lifecycle rows | 4 (connect/disconnect for both feeds) |
| Reconnects observed | 0 |
| Dropped updates / lifecycle events | 0 / 0 |
| Rolling snapshot | valid, `sample_count=480`, `coverage=1.0` |
| Smoke M / U / L | `-2.0 / 3.0 / 1.0` bps |
| Candidate signals / decay rows | 0 / 0 |

The active minute source was `/Users/lspss93119/entropy-arb-multi-market-recorder/logs/minutes-SNDK-lighter-rh.csv`. At the read-only verification point it was updated through `2026-10-24T03:38:00Z`; the two existing Recorder processes remained PID `51001` and `51004` with their original commands. No remote source file was edited.

No candidate signal occurred during this 10-second capture, so there is no real-signal 50–1000 ms decay sample to summarize and no claim about executable profitability. The tracker and horizon behavior are covered by deterministic tests; a longer capture is required for empirical decay distributions.

## Verification

- Focused quote-latency tests: **19 passed**.
- Full pytest: **115 passed, 1 failed**.
- The one failure is the pre-existing Rich width/truncation assertion `tests/test_dashboard.py::test_renders_key_numbers`; the base `e7ae4d7` baseline already showed the same failure (96 passed, 1 failed). No new quote-latency failure was introduced.
- `PYTHONPYCACHEPREFIX=/private/tmp/entropy-arb-quote-latency-pyc python3 -m compileall -q entropy_arb tools tests`: passed.
- `git diff --check`: passed.

## Known limitations and next analysis

1. Source-age values are clock differences. They do not establish one-way transport latency, clock synchronization, or exchange matching latency.
2. RH `last_updated_at` semantics and clock domain require a protocol-level confirmation before using that field as a latency benchmark; it is retained for offline audit.
3. The smoke was intentionally short and had no candidate signals. Phase 3 should use longer frozen captures, split by connection/session, and report source-time coverage, local receive skew, candidate edge distribution, and first post-signal observations separately for each horizon.
4. BBO/depth observations and candidate edges are not fills, realized PnL, or proof of executable size. No trade or PnL conclusion is made here.

Phase 1/2 delivery status: **PASS for the requested read-only research instrumentation; NOT VERIFIED for empirical edge-decay performance until a longer signal-bearing capture exists.**
