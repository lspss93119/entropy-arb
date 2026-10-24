# SNDK x RH Quote-Latency Research Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in, public-feed-only SNDK x Lighter RH high-frequency recorder that preserves timestamp semantics and measures candidate executable-edge decay without touching trading behavior.

**Architecture:** Add optional callbacks to the existing HL and Lighter public book feeds. A new pure quote-latency module normalizes feed events, calculates source/local timing and existing executable-edge formulas, tracks rising candidate signals and delayed observations, and writes bounded rotated JSONL. A small CLI wires two explicit public feeds (`io:SNDK`, RH market 32) and an optional existing LegacyRollingStrategy CSV provider.

**Tech Stack:** Python 3, asyncio, existing `websockets`/`OrderBook`/`LegacyRollingStrategy`, JSONL, pytest.

**Spec:** `docs/superpowers/specs/2026-10-24-sndk-rh-quote-latency-design.md`

## Global Constraints

- The recorder is opt-in, record-only, public websocket data only, and never imports credentials or the trading Engine.
- Existing minute Recorder files/schema, Fixed/Rolling thresholds, Inventory Ladder, IOC path, and production processes remain unchanged.
- Missing source timestamps remain null/unknown; nonce is never used as time and null is never converted to zero latency.
- Quote/depth output is bounded by rows/bytes, file count, and retention hours.
- Candidate edges are observations only; no fill, PnL, or zero-latency claim is allowed.

## Review Focus

- A source timestamp present in one protocol and absent in the other: test exact unit normalization and null age.
- Lighter snapshot/diff nonce gaps and reconnect lifecycle: test callback ordering and connection identifiers.
- A pair update arriving with an old opposite leg: test skew/eligibility is recorded and no hidden trade signal is created.
- An invalid/missing rolling snapshot: test parameter fields are marked invalid and candidate signals are suppressed.
- Rotation/retention and queue pressure: test bounded files and explicit drop counts rather than silent unbounded writes.

---

### Task 1: Feed event and timestamp audit hooks

**Files:**
- Modify: `entropy_arb/feeds.py`
- Create: `entropy_arb/quote_latency.py`
- Test: `tests/test_quote_latency.py`

**Interfaces:**
- `FeedUpdateCallback = Callable[[str, str, dict, int, int, int], None]` with venue, snapshot/diff kind, raw message, wall-clock receive ns, monotonic receive ns, and connection id.
- `FeedLifecycleCallback = Callable[[str, str, int, int, int], None]` with venue, lifecycle event, wall-clock ns, monotonic ns, and connection id.
- `extract_source_timestamp(message: dict, venue: str) -> TimestampInfo`.
- `normalize_feed_update(..., book: OrderBook, depth_levels: int) -> FeedQuote`.

- [ ] **Step 1: Write failing tests** for HL `data.time` in milliseconds, RH timestamp absence, malformed timestamp, bounded depth, and callback payloads from HL and Lighter snapshot/diff handlers.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_quote_latency.py -q`; expect failure because the callback/timestamp interfaces do not exist.
- [ ] **Step 3: Implement** optional callbacks and receive-clock capture in both feeds. Preserve existing behavior when callbacks are `None`; retain source frame fields without treating nonce as time.
- [ ] **Step 4: Run** the focused tests and confirm all timestamp/callback assertions pass.
- [ ] **Step 5: Commit** `feat: expose public quote timing events for research recorder`.

### Task 2: Pure edge decay tracker and bounded JSONL storage

**Files:**
- Modify: `entropy_arb/quote_latency.py`
- Test: `tests/test_quote_latency.py`

**Interfaces:**
- `executable_edges(entropy: FeedQuote, hedge: FeedQuote) -> EdgeObservation` using the existing recorder formulas.
- `EdgeDecayTracker.ingest(quote: FeedQuote, params: object) -> list[dict]` and `flush(now_wall_ns: int) -> list[dict]` for signals/50/100/250/500/1000ms decay records.
- `BoundedJsonlWriter.append(record: dict)`, `.close()`, and `.stats` for rotation, retention, and drop/row counts.

- [ ] **Step 1: Write failing tests** for directional edge equations, threshold crossing/latch/rearm, delayed observations, invalid snapshots, and writer rotation/retention.
- [ ] **Step 2: Run** the focused tests; expect failure on missing pure module interfaces.
- [ ] **Step 3: Implement** the pure tracker with null source age, pair skew, explicit eligibility, existing `M+U`/`L-M` hurdle semantics, and bounded file rotation.
- [ ] **Step 4: Run** focused tests and verify exact horizons and missing-observation statuses.
- [ ] **Step 5: Commit** `feat: add bounded executable-edge decay tracker`.

### Task 3: Standalone opt-in recorder CLI

**Files:**
- Create: `entropy_arb/quote_latency_recorder.py`
- Create: `tools/record_sndk_rh_quote_latency.py`
- Test: `tests/test_quote_latency_recorder.py`

**Interfaces:**
- `QuoteLatencyRecorder(..., symbol="SNDK", hedge="lighter-rh", ...)` with `async run(duration_sec: float) -> dict`.
- CLI requires explicit `--symbol SNDK` and `--hedge lighter-rh`, accepts public WS URLs, dedicated `--rolling-csv`, output/retention limits, and no credential arguments.
- Output files: rotated `quotes-*.jsonl`, `signals-*.jsonl`, `decay-*.jsonl`, `lifecycle-*.jsonl`, and `manifest.json`.

- [ ] **Step 1: Write failing tests** for explicit market validation, feed lifecycle/queue processing, manifest schemas, rolling snapshot fields, and no Engine/credential loading.
- [ ] **Step 2: Run** recorder focused tests; expect failure because the standalone recorder/CLI is absent.
- [ ] **Step 3: Implement** the async queue and two-feed orchestration, optional LegacyRollingStrategy refresh, lifecycle records, bounded writers, and end-of-run manifest. Do not call REST account APIs or Engine.
- [ ] **Step 4: Run** focused recorder tests with fake feeds and confirm no live order path is imported or invoked.
- [ ] **Step 5: Commit** `feat: add opt-in SNDK RH quote latency recorder`.

### Task 4: Research report and full verification

**Files:**
- Create: `research/sndk_rh_quote_latency_phase1_2.md`

- [ ] **Step 1: Run** focused tests, full pytest, `python3 -m compileall -q entropy_arb tools tests`, and `git diff --check`; compare any baseline Dashboard failure with exact-base evidence.
- [ ] **Step 2: Run** an independent short public websocket smoke on Mac mini in a temporary isolated directory, without touching or restarting existing Recorder processes; copy only small non-sensitive sample/manifest evidence locally.
- [ ] **Step 3: Record** actual HL/RH packet formats, timestamp units/source availability, local wall/monotonic fields, reconnect/ordering/data quality, and explicitly separate unavailable source age from zero.
- [ ] **Step 4: Write** the report with raw sample excerpts, schema/retention, tests, commit/base SHA, smoke command/result, limitations, and Phase 3 analysis method (session segmentation, latency percentiles, decay and filter retention; no PnL claim).
- [ ] **Step 5: Commit and push** only `research/sndk-rh-quote-latency-v1`, then update Issue #13 with the commit/report/test/smoke evidence.

