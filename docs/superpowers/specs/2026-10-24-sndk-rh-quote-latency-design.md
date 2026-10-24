# SNDK x RH Quote-Latency Research Design

## Intent

Provide an independent, opt-in, record-only capture path for `io:SNDK` on
Hyperliquid/Entropy and SNDK market 32 on Lighter RH. The capture is for
auditing source timestamps and observing executable-edge decay after candidate
signals; it must not become an order or strategy path.

## Goals

- Preserve each venue's raw source timestamp when present and explicitly mark
  missing or invalid source time.
- Record local wall-clock and monotonic receive times, top-of-book and bounded
  depth, reconnect/lifecycle events, rolling parameter snapshots, and candidate
  executable edges.
- Observe the first available pair state at 50, 100, 250, 500, and 1000 ms
  after a candidate threshold crossing.
- Bound output by per-file rows/bytes, file count, and retention age.
- Reuse `OrderBook`, `HLBookFeed`, `LighterBookFeed`, and the existing
  `LegacyRollingStrategy` calculation; keep the minute recorder schema and
  Engine order path unchanged.

## Non-goals and safety boundaries

- No credentials, signing, REST account calls, orders, cancellation, or Engine
  startup.
- No changes to Fixed/Rolling thresholds, Inventory Ladder, IOC, or the
  existing minute recorder output.
- No PnL, fill, or trading-performance claim from quote-only observations.

## Approach

The preferred approach is an optional callback on the existing websocket feed
consumers. The callback runs after the existing parser applies a snapshot or
diff, receives the raw frame plus precise local receive clocks, and enqueues a
small normalized event for the new recorder. This avoids a second parser and
keeps production feeds unchanged when the callback is absent.

The recorder uses two dedicated public websocket feeds, a bounded async queue,
and rotated JSONL files for quotes, candidate signals, decay observations, and
lifecycle events. A rising-edge latch avoids emitting the same candidate on
every tick while preserving a re-arm event when the edge falls below its
threshold. Rolling parameters are read through `LegacyRollingStrategy` only
when an explicit pair CSV is provided; invalid or absent snapshots produce no
candidate signal.

## Data semantics

- Hyperliquid `data.time` is retained as a source timestamp and normalized by
  magnitude to seconds/milliseconds/microseconds/nanoseconds.
- RH source time is extracted only from documented timestamp fields present in
  the frame; nonce is never treated as time. If no timestamp is present,
  `source_timestamp_ns` and source age remain null, not zero.
- Local receive age is measured from the wall-clock receive timestamp when a
  source timestamp exists; monotonic timestamps remain available for local
  ordering and duration measurement.
- Directional candidates use the existing pre-fee executable definitions:
  `entropy_bid / rh_ask - 1` for SELL entropy/BUY RH and
  `rh_bid / entropy_ask - 1` for BUY entropy/SELL RH. They are observations,
  not orders or realized results.

## Reviewable outputs

The CLI is explicit about `--symbol SNDK` and `--hedge lighter-rh`, accepts
public websocket URLs and an optional dedicated rolling CSV, and writes only
under its requested output directory. It emits a manifest with schema,
retention, source-timestamp coverage, drop/rotation counts, and lifecycle
reconnect counts. A report records live packet samples and the smoke-test
limitations without claiming zero latency or fills.
