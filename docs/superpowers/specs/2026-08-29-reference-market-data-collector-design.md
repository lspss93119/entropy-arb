# Reference Market Data Collector Design

## Status and scope

This document specifies an approved architectural feature for a research-only
reference market-data collector. The subsystem collects raw public WebSocket
reference data for Entropy, Lighter mainnet, and Lighter Robinhood so that
their oracle, index, and mark behavior can be studied offline.

The subsystem is telemetry only. It is not a strategy dependency and does not
provide values to live signal or execution code.

## Purpose

The collected data must make it possible to explain, offline:

- Entropy order-book price versus Entropy oracle and mark prices.
- Lighter order-book price versus Lighter index and mark prices.
- Lighter-RH order-book price versus Lighter-RH index and mark prices.
- Slow structural basis drift.
- The distinction between fast order-book lead-lag and reference-price basis.

This feature is limited to raw reference-data collection. It does not add or
change:

- Dynamic midline calculation.
- Oracle, index, or mark-based trading signals.
- Threshold logic.
- Execution logic.
- The samples-v2 schema or semantics.
- The minute CSV schema or semantics.
- Funding collection.
- Depth collection.
- REST polling for reference data.
- Execution telemetry.

## Verified public feeds

A credential-free, 60-second read-only probe verified the following public
feeds for `SNDK`. The observed market IDs below are evidence only; production
code must obtain market IDs from runtime market metadata.

### Entropy / Hyperliquid HIP-3

- WebSocket URL: `wss://api.hyperliquid.xyz/ws`
- Subscription type: `activeAssetCtx`
- Coin: `io:SNDK`
- Observed fields: `oraclePx`, `markPx`, and `midPx`
- Observed cadence: approximately 1 relevant frame per second
- Observed validity: all three price fields were non-null in the probe
- Observed changes: `oraclePx` and `markPx` changed only a small number of
  times during the 60-second observation
- Timestamp behavior: the relevant `activeAssetCtx` payload did not contain a
  server or source timestamp

The local receive time is an observation timestamp. It must never be described
as an oracle source timestamp.

### Lighter mainnet

- Verified `SNDK` market ID: `139` (probe evidence only)
- WebSocket channel: `market_stats/139`
- Observed fields: `index_price`, `mark_price`, `mid_price`,
  `best_bid_price`, `best_ask_price`, and top-level `timestamp`
- Timestamp behavior: the top-level timestamp was a 13-digit Unix epoch value
  in milliseconds

### Lighter Robinhood

- Verified `SNDK` market ID: `32` (probe evidence only)
- WebSocket channel: `market_stats/32`
- Observed fields and payload structure: the same schema as Lighter mainnet
- Timestamp behavior: the same top-level Unix epoch millisecond field as
  Lighter mainnet

For both Lighter deployments, the runtime venue market-loading flow remains the
source of `market_id`. The reference subsystem consumes that resolved metadata;
it must not hardcode `139`, `32`, or any symbol-specific market ID, and it must
not add a reference REST polling loop.

## Architecture

Reference collection is an independent subsystem. It is not added to the
existing trading order-book feed implementation.

```text
Engine
|
+-- Entropy l2Book WS
|     `-- trading OrderBook
|
+-- Hedge order_book WS
|     `-- trading OrderBook
|
+-- MinuteRecorder
|     +-- minutes-*.csv
|     `-- samples-v2-*.csv
|
`-- ReferenceRecorder
      +-- Entropy activeAssetCtx WS
      `-- Hedge market_stats WS
```

The reference WebSocket feeds use separate WebSocket connections from the
trading order-book feeds. Failure isolation is more important than saving a
small number of connections. A reference connection, parser, or writer failure
must not change the readiness or behavior of either trading order book.

## Module boundary

The reference subsystem belongs in a focused module:

```text
entropy_arb/reference.py
```

Its internal responsibilities are separated into these components:

- `ReferenceCsvWriter`: schema-aware append-only CSV persistence, buffering,
  header validation, disabling, and graceful close.
- `HLReferenceFeed`: the Entropy `activeAssetCtx` public WebSocket connection,
  subscription, parsing, validation, and reconnect behavior.
- `LighterReferenceFeed`: the Lighter or Lighter-RH `market_stats` public
  WebSocket connection, subscription, parsing, validation, and reconnect
  behavior.
- `ReferenceRecorder`: lifecycle coordinator for the two feeds and two writers
  owned by one process.

The reference CSV writer must not be placed in `venue_hl.py`,
`venue_lighter.py`, or `feeds.py`. Existing `feeds.py` retains its trading
order-book responsibility.

Venue objects may expose already-resolved runtime metadata to the coordinator:

- WebSocket URL.
- Hyperliquid coin.
- Lighter market ID.
- Lighter endpoint profile or hedge key.

Venue adapters do not persist reference data. The reference coordinator does
not initialize signers, inspect private credentials, or issue trading calls.

## Lifecycle

The subsystem activation matrix is exact:

| Process mode | Recorder enabled | Reference subsystem |
|---|---:|---:|
| `--record-only` | either value | ON |
| live | `true` | ON |
| live | `false` | OFF |

Reference telemetry is never a readiness gate. If either reference feed is not
ready or either writer is disabled:

- Strategy continues unchanged.
- Execution continues unchanged.
- Minute aggregation and minute CSV persistence continue unchanged.
- Samples-v2 sampling and persistence continue unchanged.
- The other reference feed and writer may continue independently.

## Per-recorder namespacing

Multiple recorder processes may run concurrently for the same symbol and
different hedge venues. Each process has its own Entropy reference feed and
writer. Processes must never share an Entropy reference file.

For configured `symbol` and `hedge_key`, every process for which the lifecycle
matrix enables reference telemetry owns exactly these two files under `logs/`:

```text
logs/reference-{symbol}-{hedge_key}-entropy.csv
logs/reference-{symbol}-{hedge_key}.csv
```

Examples:

```text
# SNDK + lighter
logs/reference-SNDK-lighter-entropy.csv
logs/reference-SNDK-lighter.csv

# SNDK + lighter-rh
logs/reference-SNDK-lighter-rh-entropy.csv
logs/reference-SNDK-lighter-rh.csv
```

The naming function uses the configured symbol and hedge key and must not
special-case `SNDK`, `lighter`, or `lighter-rh`.

The design intentionally does not add file locking, leader election, shared
writers, or cross-process coordination. A small amount of duplicated Entropy
reference data is accepted in exchange for simple ownership and failure
isolation.

## CSV schemas and timestamp semantics

### Entropy reference CSV

The exact header is:

```csv
recv_ms,oracle_px,mark_px
```

| Column | Semantics |
|---|---|
| `recv_ms` | Local Unix epoch milliseconds captured when the relevant frame is received or handled. It is an observation time, not an oracle source time. |
| `oracle_px` | Parsed positive finite numeric value from `oraclePx`. |
| `mark_px` | Parsed positive finite numeric value from `markPx`. |

### Lighter and Lighter-RH reference CSV

Both profiles use the exact same header:

```csv
recv_ms,server_ms,index_px,mark_px
```

| Column | Semantics |
|---|---|
| `recv_ms` | Local Unix epoch milliseconds captured when the relevant frame is received or handled. |
| `server_ms` | The raw top-level `market_stats` timestamp, validated as a positive Unix epoch millisecond integer. The payload does not establish a more specific event-time meaning. |
| `index_px` | Parsed positive finite numeric value from `index_price`. |
| `mark_px` | Parsed positive finite numeric value from `mark_price`. |

The collector does not persist these available fields:

- Funding fields.
- Venue-provided premium.
- `midPx` or `mid_price`.
- `best_bid_price` or `best_ask_price`.

Mid prices, BBO values, and cross-venue premium can already be reconstructed
offline from samples-v2 raw BBO data. The reference files remain narrowly
focused on values that samples-v2 cannot derive.

## Relevant frames and valid-row policy

A relevant Entropy frame is an `activeAssetCtx` update for the configured
Hyperliquid coin. A relevant Lighter frame is a `market_stats` snapshot or
update for the runtime-resolved market ID. Subscription responses without the
required data and frames for other coins, channels, or market IDs are not data
rows.

For every relevant frame, all required prices must be:

- Present.
- Non-null.
- Parseable as numeric values.
- Positive.
- Finite.

For Lighter and Lighter-RH, the top-level timestamp must also be present,
non-null, parseable as a positive integer, and consistent with Unix epoch
milliseconds. This is required to populate the exact `server_ms` schema.

If any required field is missing, null, invalid, non-positive, or non-finite:

- Log a warning with enough feed context to diagnose the frame.
- Skip the entire frame.
- Do not write a partial row.
- Keep the collector and connection running.

An irrelevant frame is ignored without a warning.

## Persistence semantics

Persistence is every-frame, not value-change-only. While its writer is enabled
and operational, every valid relevant WebSocket frame produces exactly one CSV
row even when oracle, index, or mark values are identical to the preceding row.

This preserves:

- Actual observed feed cadence.
- The difference between repeated values and a feed gap.
- Observable state age.
- Data needed for future timing research.

The subsystem must not collapse repeated values into change events.

## Buffering and append behavior

Each reference CSV writer follows these rules:

- Open in append-only mode.
- Buffer rows in process.
- Flush after every 10 written rows.
- On graceful shutdown, flush all remaining rows even when fewer than 10 are
  buffered.
- On restart, append to a valid existing file.
- Write the exact header only when the file is new or empty.
- Do not duplicate the header when appending to a valid non-empty file.

A WebSocket reconnect does not rotate the file or create a session file. The
writer continues appending to the same process-owned path. Receive and server
timestamps naturally expose gaps. A process crash may lose at most the final
unflushed buffer, which is an accepted research-data trade-off.

Reference CSV flushing occurs only in reference writer code. It is never added
to the strategy or execution hot path.

## Bad-header policy

Reference files do not reuse `MinuteRecorder`'s automatic `.old` rotation
behavior.

If a target reference CSV exists, is non-empty, and its first row is not the
exact expected header, that writer must:

- Not overwrite the file.
- Not rename or rotate the file.
- Not append any row.
- Log one clear error or warning explaining the path and expected schema.
- Become disabled for the remainder of the process.

Disabling one writer does not disable the other reference writer and does not
terminate the main process. Minute recording, samples-v2 recording, strategy,
and execution continue unchanged.

## Failure isolation

### WebSocket failures

A reference WebSocket connection failure is always non-fatal. The affected
feed reconnects with exponential delays:

```text
1s, 2s, 4s, 8s, 16s, 30s, 30s, ...
```

The delay resets after a successful connection. A reference reconnect never
marks a trading order book stale or not ready.

### Parser failures

A malformed relevant frame produces a warning, is skipped, and leaves the
connection running. An irrelevant frame is ignored. No parser exception may
escape into Engine strategy or execution tasks.

### Writer failures

If opening or writing a reference CSV fails:

- Log one clear error when that writer transitions to disabled.
- Disable only that writer for the remainder of the process.
- Do not retry the write on every frame.
- Do not emit a traceback for every later frame.
- Keep the feed, other writer, main bot, minute recorder, samples-v2 recorder,
  strategy, and execution running.

## Shutdown contract

A normal shutdown must give both reference writers an opportunity to flush
their remaining buffers. The coordinator must stop accepting new reference
rows, close the writers, and await the close operation before reference tasks
are finally discarded.

Direct task cancellation must not be the only shutdown mechanism because it
could silently discard fewer than 10 flushable rows. If a feed task must be
cancelled, writer close and its flush attempt remain part of the coordinator's
graceful shutdown path.

## Hot-path isolation

Reference state must never enter or influence:

- `_scan()`.
- `plan_arb()`.
- Threshold evaluation.
- Premium-persistence arming.
- Order sizing.
- `send_taker()`.
- Reconciliation.
- Emergency hedging.

The strategy and execution hot path must not gain:

- Synchronous REST polling.
- Synchronous reference calculations.
- Reference CSV flush operations.

No live trading logic consumes reference values in this feature.

## Existing recorder invariants

`MinuteRecorder` remains solely responsible for:

- Minute aggregation.
- Approximately 1 Hz samples-v2 collection.
- Raw BBO persistence.
- Book-update timestamps.
- Premium and executable-edge calculations.

This feature must not change:

- Minute CSV headers, cadence, naming, or semantics.
- Samples-v2 headers, cadence, naming, or semantics.
- Existing historical minute, samples, or samples-v2 files.
- Existing record-only or live recorder behavior other than independently
  starting reference telemetry under the approved lifecycle matrix.

## Test design

Future implementation must follow test-driven development. Deterministic tests
must cover at least:

1. Entropy `activeAssetCtx` parsing.
2. Lighter `market_stats` parsing.
3. Lighter-RH compatibility with the same parser.
4. Exact Entropy CSV header.
5. Exact Lighter and Lighter-RH CSV header.
6. Generalized symbol and hedge-key path naming.
7. Every-frame persistence, including consecutive unchanged values.
8. Flush after 10 rows.
9. Graceful close flush with fewer than 10 buffered rows.
10. Restart append without a duplicate header.
11. Bad-header protection without overwrite, rename, or append.
12. Malformed relevant-frame isolation.
13. Writer failure disabling only the affected writer.
14. Reference feed failure remaining non-fatal to Engine.
15. `--record-only` enabling the reference subsystem.
16. Live mode with `recorder_enabled=true` enabling the subsystem.
17. Live mode with `recorder_enabled=false` leaving the subsystem off.
18. Existing minute-recorder tests remaining green.
19. Existing samples-v2 tests remaining green.
20. Existing strategy and execution regressions remaining green.

The public-network smoke test is separate from deterministic pytest. It must be
credential-free, must not submit orders, and must confirm that the real public
subscriptions work and valid reference rows are created.

## Regression gates

Future implementation is complete only when all of these gates pass:

- Full pytest suite passes.
- `git diff --check` is clean.
- Minute schema is unchanged.
- Samples-v2 schema is unchanged.
- Strategy threshold logic is unchanged.
- Execution logic is unchanged.
- `--record-only` requires no credentials.
- Reference collection performs no REST polling.
- Credential-free public WebSocket smoke test passes.

## Expected offline research

The reference CSVs and samples-v2 raw BBO data will support as-of joins for:

- Entropy book mid minus Entropy oracle.
- Entropy book mid minus Entropy mark.
- Lighter book mid minus Lighter index.
- Lighter book mid minus Lighter mark.
- Lighter-RH book mid minus Lighter-RH index.
- Lighter-RH book mid minus Lighter-RH mark.
- Entropy oracle minus hedge index.
- Hedge mark minus hedge index.

These joins, derived values, models, and analyses are explicitly outside this
feature.

## Acceptance criteria

The feature is accepted only when:

- In normal operation with writable paths and new, empty, or valid existing
  files, each process enabled by the lifecycle matrix produces exactly two
  process-namespaced reference CSVs: one Entropy file and one hedge file. This
  includes every `--record-only` process and every live process with
  `recorder_enabled=true`.
- The Entropy file has exactly `recv_ms,oracle_px,mark_px`.
- The Lighter or Lighter-RH file has exactly
  `recv_ms,server_ms,index_px,mark_px`.
- While the corresponding writer is enabled and operational, every valid
  relevant frame is persisted, including unchanged values.
- Each writer flushes every 10 rows.
- Graceful close flushes the final buffer.
- Restart appends without duplicating the header.
- Recorder processes for different hedge keys never share reference files.
- Reference WebSocket and writer failures do not halt the bot.
- Samples-v2 behavior and schema remain unchanged.
- Minute-recorder behavior and schema remain unchanged.
- Strategy and execution behavior remain unchanged.
- No live trading logic consumes reference values.
