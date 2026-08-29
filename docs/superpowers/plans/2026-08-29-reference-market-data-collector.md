# Reference Market Data Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect research-only Entropy oracle/mark and Lighter/Lighter-RH index/mark data from independent public WebSocket feeds without affecting trading behavior.

**Architecture:** Add an isolated `entropy_arb/reference.py` subsystem owning two public WebSocket feeds and two buffered CSV writers per supported recorder process. Engine only controls lifecycle; reference traffic never touches trading order-book readiness, `_update_evt`, strategy, sizing, or execution.

**Tech Stack:** Python, asyncio, websockets, csv, pytest.

**Spec:** `docs/superpowers/specs/2026-08-29-reference-market-data-collector-design.md`

## Global Constraints

- No dynamic midline calculation.
- No oracle, index, or mark trading signal.
- No threshold changes.
- No execution, sizing, reconciliation, or emergency-hedge changes.
- No execution telemetry.
- No samples-v2 schema, cadence, naming, or historical-file changes.
- No minute schema, cadence, naming, or historical-file changes.
- No funding collection.
- No depth collection.
- No reference REST polling.
- Consume only metadata already resolved by the existing venue
  `load_market()` calls; do not add a reference-specific metadata request or
  polling loop.
- Use public WebSocket market data only.
- Reference feeds use separate WebSocket connections from trading book feeds.
- Reference readiness is never a strategy, execution, MinuteRecorder, or
  samples-v2 readiness gate.
- Each process owns its two namespaced files; add no file locks, leader
  election, shared writer, or cross-process coordination. Duplicated Entropy
  reference rows across different hedge processes are accepted.
- Every valid relevant frame is persisted, including unchanged values.
- Persist only the approved schemas; do not persist funding, `midPx`,
  `mid_price`, best bid/ask, or derived premium in reference files.
- Each CSV writer flushes after 10 data rows.
- Graceful shutdown flushes every final buffer containing fewer than 10 rows.
- Reference feed, parser, and writer failures are non-fatal.
- A non-empty bad-header file is never overwritten, renamed, rotated, or
  appended; only that writer is disabled.
- Reference traffic never calls the trading order-book `notify` callback.
- Reference traffic never sets `Engine._update_evt`.
- Reference traffic never otherwise wakes `_strategy_loop`.
- Capture `recv_ms` as Unix epoch milliseconds immediately after the raw
  message is yielded by the WebSocket iterator and before `json.loads`,
  relevance filtering, validation, logging, CSV writing, or other processing.
- Validate Lighter `server_ms` only as present, non-null, integer-parseable,
  and positive; persist the exact numeric value without comparing it with the
  local wall clock or applying a plausibility window.
- `--record-only` remains credential-free.
- Supported hedge reference collectors are exactly `lighter` and
  `lighter-rh`.
- Do not add a trade.xyz or `tradexyz` reference collector.
- Existing `tradexyz` trading and MinuteRecorder behavior remains unchanged.
- Do not invent an `xyz:SNDK` oracle or reference feed.
- Do not add config keys, CLI flags, signers, authenticated endpoints, or
  synchronous work to strategy/execution paths.
- If implementation would contradict the approved spec, stop and report the
  conflict instead of expanding scope.

## Repository Findings and Locked File Structure

Current code already supplies all runtime metadata after
`await asyncio.gather(self.entropy.load_market(), self.hedge.load_market())`:

- `HLVenue.ws_url` and `HLVenue.coin` are populated in
  `entropy_arb/venue_hl.py:62-69,99-123`.
- `LighterVenue.profile.ws_url` and `LighterVenue.market_id` are populated in
  `entropy_arb/venue_lighter.py:142-169,182-201`.
- `Config.recorder_enabled` and `Config.hedge_venue` already express the
  approved lifecycle decision in `entropy_arb/config.py:98-140,345-375`.
- Engine creates trading book tasks with `self._update_evt.set` and then
  generically cancels its task list in `entropy_arb/engine.py:189-224`.
  ReferenceRecorder must therefore use a separate task variable that Engine
  awaits rather than generically cancels.
- Existing tests use `asyncio.run()` and pytest built-ins; no async pytest
  plugin should be added.

The implementation file set is locked to:

| Action | File | Responsibility |
|---|---|---|
| Create | `entropy_arb/reference.py` | Headers, paths, CSV writer, pure parsers, independent public WS feeds, and coordinator |
| Create | `tests/test_reference.py` | Deterministic unit tests for persistence, parsing, feed loops, isolation, and shutdown |
| Modify | `entropy_arb/engine.py` | Supported-hedge activation and lifecycle ownership only |
| Modify if lifecycle wiring requires it | `tests/test_engine.py` | Activation, runtime metadata, non-fatal failure, no-wakeup, and shutdown integration |

Do not modify `entropy_arb/recorder.py`, `entropy_arb/feeds.py`,
`entropy_arb/config.py`, `entropy_arb/venue_hl.py`, or
`entropy_arb/venue_lighter.py`. If an executor finds one of those changes
unavoidable, stop and report the concrete conflict before editing it.

## Acceptance-Criterion Mapping

| Approved acceptance criterion | Implemented and verified in |
|---|---|
| Exactly two namespaced reference files per enabled supported process | Tasks 1, 4, 5, and 6 |
| Exact Entropy header `recv_ms,oracle_px,mark_px` | Tasks 1 and 6 |
| Exact Lighter/RH header `recv_ms,server_ms,index_px,mark_px` | Tasks 1 and 6 |
| Every valid relevant frame, including unchanged values, is persisted | Tasks 2 and 3 |
| Flush every 10 rows | Task 1 |
| Graceful final flush | Tasks 1 and 4 |
| Restart append without a duplicate header | Task 1 |
| Different hedge processes never share reference files | Tasks 1, 5, and 6 |
| Reference WS/writer failures never halt the bot | Tasks 1, 3, 4, and 5 |
| Reference traffic never wakes strategy scheduling | Tasks 3 and 5 |
| Samples-v2 behavior and schema remain unchanged | Task 6 |
| Minute behavior and schema remain unchanged | Task 6 |
| Strategy and execution behavior remain unchanged | Tasks 5 and 6 |
| No live trading logic consumes reference values | Tasks 3, 5, and 6 |
| `recv_ms` is captured before parsing or processing | Task 3 |
| Lighter `server_ms` has no wall-clock plausibility gate | Task 2 |
| Writer close is idempotent and shutdown cannot write after close | Tasks 1 and 4 |
| `--record-only` needs no credentials | Tasks 5 and 6 |
| No reference REST polling | Tasks 3, 5, and 6 |
| `tradexyz` remains outside the feature | Tasks 5 and 6 |

---

### Task 1: Reference Paths and CSV Writer

**Files:**
- Create: `entropy_arb/reference.py`
- Create: `tests/test_reference.py`

**Interfaces:**
- Consumes: Python `csv`, `logging`, and `os` only.
- Produces:

```python
ENTROPY_REFERENCE_HEADER: tuple[str, ...] = (
    "recv_ms", "oracle_px", "mark_px",
)
LIGHTER_REFERENCE_HEADER: tuple[str, ...] = (
    "recv_ms", "server_ms", "index_px", "mark_px",
)

def reference_paths(
    symbol: str,
    hedge_key: str,
    directory: str = "logs",
) -> tuple[str, str]:
    """Return (entropy_path, hedge_path)."""

class ReferenceCsvWriter:
    def __init__(
        self,
        path: str,
        header: tuple[str, ...],
        flush_rows: int = 10,
    ) -> None:
        """Open or disable without raising an Engine-fatal I/O exception."""

    @property
    def enabled(self) -> bool:
        """True only while writes can be accepted."""

    def write(self, row: tuple[object, ...]) -> None:
        """Write one row or no-op when disabled/stopped."""

    def stop_accepting(self) -> None:
        """Idempotently reject later rows without closing the current buffer."""

    def close(self) -> None:
        """Idempotently flush the remainder and close."""
```

- Later tasks rely on `stop_accepting()` to close the write-after-close race
  before feed tasks terminate.

- [ ] **Step 1: Add RED tests for exact headers and generalized paths**

Add these tests first:

```python
import csv
import logging
import os

import pytest

from entropy_arb.reference import (
    ENTROPY_REFERENCE_HEADER,
    LIGHTER_REFERENCE_HEADER,
    reference_paths,
)


def test_reference_headers_are_exact():
    assert ENTROPY_REFERENCE_HEADER == (
        "recv_ms", "oracle_px", "mark_px",
    )
    assert LIGHTER_REFERENCE_HEADER == (
        "recv_ms", "server_ms", "index_px", "mark_px",
    )


@pytest.mark.parametrize(
    ("symbol", "hedge_key", "expected"),
    [
        (
            "SNDK",
            "lighter",
            (
                "logs/reference-SNDK-lighter-entropy.csv",
                "logs/reference-SNDK-lighter.csv",
            ),
        ),
        (
            "SNDK",
            "lighter-rh",
            (
                "logs/reference-SNDK-lighter-rh-entropy.csv",
                "logs/reference-SNDK-lighter-rh.csv",
            ),
        ),
        (
            "ETH",
            "future-lighter-profile",
            (
                "logs/reference-ETH-future-lighter-profile-entropy.csv",
                "logs/reference-ETH-future-lighter-profile.csv",
            ),
        ),
    ],
)
def test_reference_paths_are_namespaced(symbol, hedge_key, expected):
    assert reference_paths(symbol, hedge_key) == expected
```

- [ ] **Step 2: Run the path/header test and record RED**

Run:

```bash
python3 -m pytest tests/test_reference.py::test_reference_headers_are_exact tests/test_reference.py::test_reference_paths_are_namespaced -q
```

Expected RED: collection fails with
`ModuleNotFoundError: No module named 'entropy_arb.reference'`.

- [ ] **Step 3: Add the minimal constants and path function**

Create `entropy_arb/reference.py` with the exact tuples above and:

```python
from __future__ import annotations

import csv
import logging
import os

log = logging.getLogger("reference")

ENTROPY_REFERENCE_HEADER = ("recv_ms", "oracle_px", "mark_px")
LIGHTER_REFERENCE_HEADER = ("recv_ms", "server_ms", "index_px", "mark_px")


def reference_paths(
    symbol: str,
    hedge_key: str,
    directory: str = "logs",
) -> tuple[str, str]:
    entropy_name = f"reference-{symbol}-{hedge_key}-entropy.csv"
    hedge_name = f"reference-{symbol}-{hedge_key}.csv"
    return (
        os.path.join(directory, entropy_name),
        os.path.join(directory, hedge_name),
    )
```

- [ ] **Step 4: Run the path/header tests GREEN**

Run the Step 2 command again.

Expected GREEN: both selected tests pass with zero failures.

- [ ] **Step 5: Add RED tests for every-call persistence, buffering, close, and append**

Extend the reference import with `ReferenceCsvWriter`, then add concrete
helpers and tests:

```python
def read_rows(path):
    with open(path, newline="") as fh:
        return list(csv.reader(fh))


def test_writer_persists_identical_rows_independently(tmp_path):
    path = tmp_path / "reference.csv"
    writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    row = (1_787_993_704_054, 1485.0, 1485.0)
    writer.write(row)
    writer.write(row)
    writer.close()
    assert read_rows(path) == [
        list(ENTROPY_REFERENCE_HEADER),
        [str(value) for value in row],
        [str(value) for value in row],
    ]


def test_writer_flushes_row_ten_and_close_flushes_remainder(tmp_path):
    path = tmp_path / "reference.csv"
    writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    for recv_ms in range(1, 10):
        writer.write((recv_ms, 100.0, 101.0))
    assert read_rows(path) == [list(ENTROPY_REFERENCE_HEADER)]

    writer.write((10, 100.0, 101.0))
    assert len(read_rows(path)) == 11

    writer.write((11, 100.0, 101.0))
    writer.write((12, 100.0, 101.0))
    assert len(read_rows(path)) == 11
    writer.close()
    assert len(read_rows(path)) == 13


def test_writer_restart_appends_one_header(tmp_path):
    path = tmp_path / "reference.csv"
    for recv_ms in (1, 2):
        writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
        writer.write((recv_ms, 100.0, 101.0))
        writer.close()
    rows = read_rows(path)
    assert rows[0] == list(ENTROPY_REFERENCE_HEADER)
    assert rows.count(list(ENTROPY_REFERENCE_HEADER)) == 1
    assert len(rows) == 3


def test_stop_accepting_rejects_late_rows_but_close_flushes_prior_rows(tmp_path):
    path = tmp_path / "reference.csv"
    writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    writer.write((1, 100.0, 101.0))
    writer.stop_accepting()
    writer.write((2, 102.0, 103.0))
    writer.close()
    assert read_rows(path) == [
        list(ENTROPY_REFERENCE_HEADER),
        ["1", "100.0", "101.0"],
    ]
```

- [ ] **Step 6: Run the writer behavior tests and record RED**

Run:

```bash
python3 -m pytest tests/test_reference.py -k 'writer_persists or writer_flushes or writer_restart or stop_accepting' -q
```

Expected RED: collection fails because `ReferenceCsvWriter` is not defined.

- [ ] **Step 7: Implement only the successful buffered append path**

Implement the successful path first so the Step 5 behavior tests pass while
bad-header and I/O containment remain observably RED:

```python
class ReferenceCsvWriter:
    def __init__(
        self,
        path: str,
        header: tuple[str, ...],
        flush_rows: int = 10,
    ) -> None:
        self.path = path
        self.header = header
        self.flush_rows = flush_rows
        self._enabled = False
        self._accepting = False
        self._closed = False
        self._pending = 0
        self._fh = None
        self._writer = None
        self._open()

    @property
    def enabled(self) -> bool:
        return self._enabled and self._accepting and not self._closed

    def _open(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        write_header = not os.path.exists(self.path) or os.path.getsize(
            self.path
        ) == 0
        self._fh = open(self.path, "a", newline="")
        self._writer = csv.writer(self._fh)
        if write_header:
            self._writer.writerow(self.header)
            self._fh.flush()
        self._enabled = True
        self._accepting = True

    def write(self, row: tuple[object, ...]) -> None:
        if not self.enabled:
            return
        self._writer.writerow(row)
        self._pending += 1
        if self._pending >= self.flush_rows:
            self._fh.flush()
            self._pending = 0

    def stop_accepting(self) -> None:
        self._accepting = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.stop_accepting()
        handle = self._fh
        self._enabled = False
        self._fh = None
        self._writer = None
        if handle is not None and self._pending:
            handle.flush()
        self._pending = 0
        if handle is not None:
            handle.close()
```

This deliberately does not yet validate an existing header or contain I/O
errors. Those frozen contracts are driven RED in Step 9 before Step 11 adds
them.

- [ ] **Step 8: Run the writer behavior tests GREEN**

Run the Step 6 command again.

Expected GREEN: every selected writer behavior test passes; zero failures.

- [ ] **Step 9: Add RED tests for bad headers, contained I/O errors, and idempotent close**

Add:

```python
def test_bad_header_disables_without_touching_file(tmp_path, caplog):
    path = tmp_path / "reference.csv"
    original = b"wrong,header\n1,2\n"
    path.write_bytes(original)
    with caplog.at_level(logging.ERROR, logger="reference"):
        writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    assert writer.enabled is False
    writer.write((3, 100.0, 101.0))
    writer.close()
    assert path.read_bytes() == original
    assert not (tmp_path / "reference.csv.old").exists()
    assert sum(
        "header validation" in record.message for record in caplog.records
    ) == 1


def test_write_error_logs_once_and_disables(tmp_path, caplog):
    class BrokenCsvWriter:
        def writerow(self, row):
            raise OSError("disk full")

    path = tmp_path / "reference.csv"
    writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    writer._writer = BrokenCsvWriter()
    with caplog.at_level(logging.ERROR, logger="reference"):
        writer.write((1, 100.0, 101.0))
        writer.write((2, 100.0, 101.0))
        writer.close()
    assert writer.enabled is False
    assert sum("disk full" in record.message for record in caplog.records) == 1


def test_open_error_is_contained_and_logged_once(tmp_path, monkeypatch, caplog):
    target = tmp_path / "blocked" / "reference.csv"

    def fail_makedirs(path, exist_ok):
        raise OSError("read-only directory")

    monkeypatch.setattr(os, "makedirs", fail_makedirs)
    with caplog.at_level(logging.ERROR, logger="reference"):
        writer = ReferenceCsvWriter(str(target), ENTROPY_REFERENCE_HEADER)
        writer.write((1, 100.0, 101.0))
        writer.close()
    assert writer.enabled is False
    assert sum(
        "read-only directory" in record.message for record in caplog.records
    ) == 1


def test_close_is_idempotent(tmp_path):
    path = tmp_path / "reference.csv"
    writer = ReferenceCsvWriter(str(path), ENTROPY_REFERENCE_HEADER)
    writer.write((1, 100.0, 101.0))
    writer.close()
    before = path.read_bytes()
    writer.close()
    writer.write((2, 102.0, 103.0))
    assert path.read_bytes() == before
```

- [ ] **Step 10: Run the failure-policy tests and record RED**

Run:

```bash
python3 -m pytest tests/test_reference.py -k 'bad_header or error or idempotent' -q
```

Expected RED: one or more assertions show that bad-header/error/idempotent
behavior is not yet complete; no test is allowed to pass by rotating a file or
letting an I/O exception escape.

- [ ] **Step 11: Complete writer disable and close behavior**

Add `self._error_logged = False`, then replace `_open()` and add `_disable()`:

```python
def _open(self) -> None:
    try:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        write_header = (
            not os.path.exists(self.path)
            or os.path.getsize(self.path) == 0
        )
        if not write_header:
            with open(self.path, newline="") as existing:
                actual = next(csv.reader(existing), None)
            if tuple(actual or ()) != self.header:
                self._disable(
                    "header validation",
                    ValueError(
                        f"expected {self.header!r}, got {actual!r}"
                    ),
                )
                return
        self._fh = open(self.path, "a", newline="")
        self._writer = csv.writer(self._fh)
        if write_header:
            self._writer.writerow(self.header)
            self._fh.flush()
        self._enabled = True
        self._accepting = True
    except (OSError, csv.Error, UnicodeError) as exc:
        self._disable("open", exc)


def _disable(self, operation: str, error: BaseException | str) -> None:
    if not self._error_logged:
        log.error(
            "reference writer disabled path=%s operation=%s error=%s",
            self.path,
            operation,
            error,
        )
        self._error_logged = True
    self._enabled = False
    self._accepting = False
    handle = self._fh
    self._fh = None
    self._writer = None
    self._pending = 0
    if handle is not None:
        try:
            handle.close()
        except OSError:
            return
```

Replace `write()` and `close()` with:

```python
def write(self, row: tuple[object, ...]) -> None:
    if not self.enabled:
        return
    try:
        self._writer.writerow(row)
        self._pending += 1
        if self._pending >= self.flush_rows:
            self._fh.flush()
            self._pending = 0
    except (OSError, csv.Error) as exc:
        self._disable("write", exc)


def close(self) -> None:
    if self._closed:
        return
    self._closed = True
    self.stop_accepting()
    handle = self._fh
    self._enabled = False
    self._fh = None
    self._writer = None
    try:
        if handle is not None and self._pending:
            handle.flush()
    except (OSError, csv.Error) as exc:
        self._disable("close flush", exc)
    finally:
        self._pending = 0
        if handle is not None:
            try:
                handle.close()
            except OSError as exc:
                self._disable("close", exc)
```

This completes exact header validation, one transition log, row-ten flush,
final remainder flush, and the no-rotate policy without changing the
already-GREEN success semantics.

- [ ] **Step 12: Run all Task 1 tests GREEN**

Run:

```bash
python3 -m pytest tests/test_reference.py -q
```

Expected GREEN: all currently collected reference path/writer tests pass with
zero failures.

- [ ] **Step 13: Commit Task 1**

```bash
git add entropy_arb/reference.py tests/test_reference.py
git commit -m "feat: add reference csv writer"
```

---

### Task 2: Pure Reference Payload Parsing

**Files:**
- Modify: `entropy_arb/reference.py`
- Modify: `tests/test_reference.py`

**Interfaces:**
- Consumes: headers/writer from Task 1 only by sharing the module.
- Produces:

```python
class ReferenceParseError(ValueError):
    """A relevant reference frame cannot produce a complete valid row."""


def parse_hl_reference(
    msg: dict,
    *,
    coin: str,
) -> tuple[float, float] | None:
    """Return (oracle_px, mark_px), None for irrelevant, or raise."""


def parse_lighter_reference(
    msg: dict,
    *,
    market_id: int,
) -> tuple[int, float, float] | None:
    """Return (server_ms, index_px, mark_px), None for irrelevant, or raise."""
```

- Task 3 consumes these exact signatures and catches
  `ReferenceParseError` without closing the socket.

- [ ] **Step 1: Add sanitized probe fixtures and RED happy-path tests**

Extend the `entropy_arb.reference` test import with `ReferenceParseError`,
`parse_hl_reference`, and `parse_lighter_reference`, then add exact public
payload shapes:

```python
HL_REFERENCE_FRAME = {
    "channel": "activeAssetCtx",
    "data": {
        "coin": "io:SNDK",
        "ctx": {
            "oraclePx": "1485.0",
            "markPx": "1485.0",
            "midPx": "1483.3",
            "funding": "0.0000015625",
        },
    },
}

LIGHTER_REFERENCE_FRAME = {
    "channel": "market_stats:139",
    "market_stats": {
        "market_id": 139,
        "symbol": "SNDK",
        "index_price": "1488.07",
        "mark_price": "1483.77",
        "mid_price": "1483.83",
        "best_bid_price": "1483.74",
        "best_ask_price": "1483.92",
        "funding_rate": "0.0004",
    },
    "timestamp": 1_787_993_704_054,
    "type": "subscribed/market_stats",
}


def test_parse_hl_reference_from_probe_shape():
    assert parse_hl_reference(
        HL_REFERENCE_FRAME, coin="io:SNDK"
    ) == (1485.0, 1485.0)


@pytest.mark.parametrize(
    "message_type",
    ["subscribed/market_stats", "update/market_stats"],
)
@pytest.mark.parametrize("market_id", [139, 32])
def test_parse_lighter_reference_for_mainnet_and_rh(
        message_type, market_id):
    msg = {
        **LIGHTER_REFERENCE_FRAME,
        "channel": f"market_stats:{market_id}",
        "market_stats": {
            **LIGHTER_REFERENCE_FRAME["market_stats"],
            "market_id": market_id,
        },
        "type": message_type,
    }
    assert parse_lighter_reference(msg, market_id=market_id) == (
        1_787_993_704_054,
        1488.07,
        1483.77,
    )


def test_lighter_positive_integer_timestamp_has_no_wall_clock_gate():
    msg = {**LIGHTER_REFERENCE_FRAME, "timestamp": 1}
    assert parse_lighter_reference(msg, market_id=139)[0] == 1
```

- [ ] **Step 2: Run happy-path parser tests and record RED**

Run:

```bash
python3 -m pytest tests/test_reference.py -k 'parse_hl_reference_from or parse_lighter_reference_for or no_wall_clock_gate' -q
```

Expected RED: import or collection fails because parser interfaces are absent.

- [ ] **Step 3: Implement only valid-frame parsing**

Add the exception type and the smallest happy-path parser behavior:

```python
class ReferenceParseError(ValueError):
    """A relevant frame cannot produce a complete valid row."""


def parse_hl_reference(msg: dict, *, coin: str):
    if msg.get("channel") != "activeAssetCtx":
        return None
    data = msg["data"]
    if data.get("coin") != coin:
        return None
    ctx = data["ctx"]
    return float(ctx["oraclePx"]), float(ctx["markPx"])


def parse_lighter_reference(msg: dict, *, market_id: int):
    if msg.get("type") not in {
        "subscribed/market_stats",
        "update/market_stats",
    }:
        return None
    stats = msg["market_stats"]
    if int(stats["market_id"]) != market_id:
        return None
    return (
        int(msg["timestamp"]),
        float(stats["index_price"]),
        float(stats["mark_price"]),
    )
```

This intentionally accepts some invalid numeric values and may leak built-in
conversion/key exceptions. Step 5 records those gaps as RED before Step 7 adds
the complete validation boundary.

- [ ] **Step 4: Run happy-path parser tests GREEN**

Run the Step 2 command again.

Expected GREEN: all selected happy-path tests pass; zero failures.

- [ ] **Step 5: Add RED invalid/irrelevant parser cases**

Add:

```python
import copy


def test_hl_wrong_coin_and_irrelevant_channel_return_none():
    wrong_coin = copy.deepcopy(HL_REFERENCE_FRAME)
    wrong_coin["data"]["coin"] = "io:BTC"
    assert parse_hl_reference(wrong_coin, coin="io:SNDK") is None
    assert parse_hl_reference(
        {"channel": "pong"}, coin="io:SNDK"
    ) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("oraclePx", None),
        ("oraclePx", 0),
        ("oraclePx", -1),
        ("oraclePx", "NaN"),
        ("markPx", "Infinity"),
    ],
)
def test_hl_relevant_invalid_price_raises(field, value):
    msg = copy.deepcopy(HL_REFERENCE_FRAME)
    msg["data"]["ctx"][field] = value
    with pytest.raises(ReferenceParseError):
        parse_hl_reference(msg, coin="io:SNDK")


def test_lighter_wrong_market_returns_none():
    assert parse_lighter_reference(
        LIGHTER_REFERENCE_FRAME, market_id=32
    ) is None


def test_lighter_control_ack_without_market_payload_is_irrelevant():
    assert parse_lighter_reference(
        {"type": "subscribed/market_stats"}, market_id=139
    ) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("timestamp", None),
        ("timestamp", 0),
        ("timestamp", -1),
        ("timestamp", 1.5),
        ("index_price", None),
        ("index_price", 0),
        ("index_price", -1),
        ("index_price", "NaN"),
        ("mark_price", "Infinity"),
    ],
)
def test_lighter_relevant_invalid_required_field_raises(field, value):
    msg = copy.deepcopy(LIGHTER_REFERENCE_FRAME)
    if field == "timestamp":
        msg[field] = value
    else:
        msg["market_stats"][field] = value
    with pytest.raises(ReferenceParseError):
        parse_lighter_reference(msg, market_id=139)


@pytest.mark.parametrize(
    ("parser", "kwargs", "path"),
    [
        (parse_hl_reference, {"coin": "io:SNDK"}, ("data", "ctx", "oraclePx")),
        (
            parse_lighter_reference,
            {"market_id": 139},
            ("market_stats", "mark_price"),
        ),
    ],
)
def test_missing_required_field_raises(parser, kwargs, path):
    source = (
        HL_REFERENCE_FRAME
        if parser is parse_hl_reference
        else LIGHTER_REFERENCE_FRAME
    )
    msg = copy.deepcopy(source)
    parent = msg
    for key in path[:-1]:
        parent = parent[key]
    del parent[path[-1]]
    with pytest.raises(ReferenceParseError):
        parser(msg, **kwargs)
```

- [ ] **Step 6: Run invalid/irrelevant tests and record RED**

Run:

```bash
python3 -m pytest tests/test_reference.py -k 'wrong_coin or invalid_price or wrong_market or control_ack or invalid_required or missing_required' -q
```

Expected RED: incomplete classification or validation produces assertion
failures; no NaN, infinity, zero, negative, null, or missing required field may
be accepted.

- [ ] **Step 7: Complete parser validation**

Add `math` and `typing.Any`, then use these helpers:

```python
def _positive_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ReferenceParseError(f"{field} must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReferenceParseError(f"{field} must be numeric") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ReferenceParseError(f"{field} must be positive and finite")
    return parsed


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ReferenceParseError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReferenceParseError(f"{field} must be an integer") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ReferenceParseError(f"{field} must be an integer")
    if parsed <= 0:
        raise ReferenceParseError(f"{field} must be positive")
    return parsed
```

Complete both parsers with these exact rules:

1. HL channels other than `activeAssetCtx` and Lighter message types other
   than `subscribed/market_stats` or `update/market_stats` return `None`.
2. A bare subscription/control acknowledgement with no market payload or
   usable market identity returns `None`; it is not a malformed data row.
3. A different explicit `data.coin`, channel market ID, or payload
   `market_id` returns `None` without warning.
4. Once a frame identifies the configured coin/market, missing/non-dict
   containers, missing identity, or a
   missing/invalid required field raises `ReferenceParseError`.
5. Accept Lighter channel forms `market_stats:<id>` and
   `market_stats/<id>`; require channel/payload identifiers to agree when both
   are present.
6. Ignore `midPx`, `mid_price`, BBO, funding, and all other non-required
   fields.
7. Return the positive integer `timestamp` without a clock call, comparison,
   rescale, offset, clamp, or wall-clock plausibility gate.

Keep both functions pure: no logging, clock, network, writer, Engine, or global
mutable state.

- [ ] **Step 8: Run all Task 1 and Task 2 tests GREEN**

Run:

```bash
python3 -m pytest tests/test_reference.py -q
```

Expected GREEN: all current writer and parser tests pass; zero failures.

- [ ] **Step 9: Commit Task 2**

```bash
git add entropy_arb/reference.py tests/test_reference.py
git commit -m "feat: parse reference market data"
```

---

### Task 3: Independent Public WebSocket Feed Loops

**Files:**
- Modify: `entropy_arb/reference.py`
- Modify: `tests/test_reference.py`

**Interfaces:**
- Consumes:
  - `ReferenceCsvWriter`
  - `ReferenceParseError`
  - `parse_hl_reference(msg, *, coin)`
  - `parse_lighter_reference(msg, *, market_id)`
- Produces:

```python
class HLReferenceFeed:
    def __init__(
        self,
        name: str,
        ws_url: str,
        coin: str,
        writer: ReferenceCsvWriter,
        *,
        connect=ws_connect,
        clock_ns=time.time_ns,
        sleep=asyncio.sleep,
    ) -> None:
        self.name = name
        self.ws_url = ws_url
        self.coin = coin
        self.writer = writer
        self._connect = connect
        self._clock_ns = clock_ns
        self._sleep = sleep

    async def _pinger(self, ws) -> None:
        """Send reference-connection application pings every five seconds."""

    async def run(self, stop: asyncio.Event) -> None:
        """Consume activeAssetCtx frames with isolated reconnect handling."""


class LighterReferenceFeed:
    def __init__(
        self,
        name: str,
        ws_url: str,
        market_id: int,
        writer: ReferenceCsvWriter,
        *,
        connect=ws_connect,
        clock_ns=time.time_ns,
        sleep=asyncio.sleep,
    ) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.writer = writer
        self._connect = connect
        self._clock_ns = clock_ns
        self._sleep = sleep

    async def _subscribe(self, ws) -> None:
        """Subscribe to the runtime market_stats channel."""

    async def run(self, stop: asyncio.Event) -> None:
        """Consume market_stats frames with isolated reconnect handling."""
```

The method signatures above are the complete public/test seams; Steps 4, 8,
and 12 fill their protocol behavior. Neither constructor accepts an OrderBook,
`notify` callback, Engine, event, credential, HTTP session, or signer.

- [ ] **Step 1: Add reusable deterministic fake WebSocket helpers**

Extend the reference import with `HLReferenceFeed` and
`LighterReferenceFeed`, then add to `tests/test_reference.py`:

```python
import asyncio
import inspect
import json


class StubReferenceWriter:
    def __init__(self):
        self.enabled = True
        self.rows = []

    def write(self, row):
        if self.enabled:
            self.rows.append(row)


class FakeWebSocket:
    def __init__(self, frames, stop):
        self.frames = list(frames)
        self.stop = stop
        self.sent = []
        self.decode_sent = json.loads

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.frames:
            return self.frames.pop(0)
        self.stop.set()
        raise StopAsyncIteration

    async def send(self, raw):
        self.sent.append(self.decode_sent(raw))


class FakeConnect:
    def __init__(self, websocket):
        self.websocket = websocket
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.websocket
```

- [ ] **Step 2: Add RED tests for subscriptions, every-frame writes, receive ordering, and constructor isolation**

Add tests that run via `asyncio.run()`:

```python
def test_hl_feed_subscribes_and_persists_identical_frames():
    async def scenario():
        stop = asyncio.Event()
        frames = [
            json.dumps(HL_REFERENCE_FRAME),
            json.dumps(HL_REFERENCE_FRAME),
        ]
        ws = FakeWebSocket(frames, stop)
        writer = StubReferenceWriter()
        feed = HLReferenceFeed(
            "ENTROPY",
            "wss://api.hyperliquid.xyz/ws",
            "io:SNDK",
            writer,
            connect=FakeConnect(ws),
            clock_ns=lambda: 1_700_000_000_123_456_789,
        )
        await feed.run(stop)
        assert ws.sent[0] == {
            "method": "subscribe",
            "subscription": {
                "type": "activeAssetCtx",
                "coin": "io:SNDK",
            },
        }
        assert writer.rows == [
            (1_700_000_000_123, 1485.0, 1485.0),
            (1_700_000_000_123, 1485.0, 1485.0),
        ]

    asyncio.run(scenario())


def test_lighter_feed_subscribes_after_connected_frame():
    async def scenario():
        stop = asyncio.Event()
        ws = FakeWebSocket(
            [
                json.dumps({"type": "connected"}),
                json.dumps(LIGHTER_REFERENCE_FRAME),
            ],
            stop,
        )
        writer = StubReferenceWriter()
        feed = LighterReferenceFeed(
            "LIGHTER",
            "wss://mainnet.zklighter.elliot.ai/stream",
            139,
            writer,
            connect=FakeConnect(ws),
            clock_ns=lambda: 1_700_000_000_999_000_000,
        )
        await feed.run(stop)
        assert ws.sent[0] == {
            "type": "subscribe",
            "channel": "market_stats/139",
        }
        assert writer.rows == [
            (1_700_000_000_999, 1_787_993_704_054, 1488.07, 1483.77),
        ]

    asyncio.run(scenario())


def test_recv_ms_is_captured_before_json_and_parser(monkeypatch):
    async def scenario():
        from entropy_arb import reference

        stop = asyncio.Event()
        ws = FakeWebSocket([json.dumps(HL_REFERENCE_FRAME)], stop)
        writer = StubReferenceWriter()
        calls = []
        real_loads = json.loads
        real_parser = reference.parse_hl_reference

        def clock_ns():
            calls.append("clock")
            return 1_700_000_000_123_000_000

        def tracked_loads(raw):
            calls.append("json")
            return real_loads(raw)

        def tracked_parser(msg, *, coin):
            calls.append("parser")
            return real_parser(msg, coin=coin)

        monkeypatch.setattr(reference.json, "loads", tracked_loads)
        monkeypatch.setattr(reference, "parse_hl_reference", tracked_parser)
        original_write = writer.write

        def tracked_write(row):
            calls.append("write")
            original_write(row)

        writer.write = tracked_write
        feed = HLReferenceFeed(
            "ENTROPY", "wss://example.invalid/ws", "io:SNDK", writer,
            connect=FakeConnect(ws), clock_ns=clock_ns,
        )
        await feed.run(stop)
        assert calls[:4] == ["clock", "json", "parser", "write"]

    asyncio.run(scenario())


def test_reference_feeds_have_no_trading_notify_dependency():
    assert "notify" not in inspect.signature(HLReferenceFeed).parameters
    assert "notify" not in inspect.signature(LighterReferenceFeed).parameters
    assert "book" not in inspect.signature(HLReferenceFeed).parameters
    assert "book" not in inspect.signature(LighterReferenceFeed).parameters
```

- [ ] **Step 3: Run subscription/write tests and record RED**

Run:

```bash
python3 -m pytest tests/test_reference.py -k 'feed_subscribes or captured_before or no_trading_notify' -q
```

Expected RED: collection fails because `HLReferenceFeed` and
`LighterReferenceFeed` are not defined.

- [ ] **Step 4: Implement valid-frame subscription and persistence with receive-time first**

Import `asyncio`, `json`, `time`, and the same `ws_connect` compatibility
fallback currently used by `feeds.py`. Each feed invokes its injected
connector as `connect(ws_url, max_size=2**23, open_timeout=10,
ping_interval=15, ping_timeout=15)` on its own connection. The first statement
inside each `async for raw in ws` body must be:

```python
recv_ms = self._clock_ns() // 1_000_000
```

Only after that statement may the loop reset backoff, call `json.loads`,
inspect message type, parse fields, log, or write.

Use this initial valid-frame handling shape in both feeds:

```python
async for raw in ws:
    recv_ms = self._clock_ns() // 1_000_000
    backoff = 1.0
    msg = json.loads(raw)
    parsed = parse_hl_reference(msg, coin=self.coin)
    if parsed is not None and self.writer.enabled:
        self.writer.write((recv_ms, *parsed))
    if stop.is_set():
        break
```

The Lighter loop substitutes `parse_lighter_reference` and handles protocol
frames before parsing:

- On `connected`, send
  `{"type": "subscribe", "channel": f"market_stats/{self.market_id}"}`.
- On `ping`, send `{"type": "pong"}`.
- Parse subscribed/update market-stats frames through the pure parser.

The HL loop sends the approved `activeAssetCtx` subscription immediately after
connect. Add an internal application pinger that sends
`{"method": "ping"}` every 5 seconds and is cancelled/awaited when the
connection ends. This pinger is reference-only and has no callback.

- [ ] **Step 5: Run valid-frame, ordering, and constructor-isolation tests GREEN**

Run the Step 3 command again.

Expected GREEN: subscriptions, duplicate rows, receive-time ordering, and
absence of trading callback/book dependencies all pass with zero failures.

- [ ] **Step 6: Add RED malformed/irrelevant frame-isolation tests**

Add:

```python
def test_malformed_relevant_frame_is_skipped_without_ending_loop(caplog):
    async def scenario():
        stop = asyncio.Event()
        invalid = copy.deepcopy(HL_REFERENCE_FRAME)
        invalid["data"]["ctx"]["oraclePx"] = None
        ws = FakeWebSocket(
            [json.dumps(invalid), json.dumps(HL_REFERENCE_FRAME)],
            stop,
        )
        writer = StubReferenceWriter()
        feed = HLReferenceFeed(
            "ENTROPY", "wss://example.invalid/ws", "io:SNDK", writer,
            connect=FakeConnect(ws),
        )
        await feed.run(stop)
        assert len(writer.rows) == 1

    with caplog.at_level(logging.WARNING, logger="reference"):
        asyncio.run(scenario())
    assert "malformed relevant reference frame" in caplog.text


def test_irrelevant_frames_do_not_write(caplog):
    async def scenario():
        stop = asyncio.Event()
        ws = FakeWebSocket(
            [
                json.dumps({"channel": "pong"}),
                json.dumps({
                    **LIGHTER_REFERENCE_FRAME,
                    "channel": "market_stats:32",
                    "market_stats": {
                        **LIGHTER_REFERENCE_FRAME["market_stats"],
                        "market_id": 32,
                    },
                }),
            ],
            stop,
        )
        writer = StubReferenceWriter()
        feed = LighterReferenceFeed(
            "LIGHTER", "wss://example.invalid/ws", 139, writer,
            connect=FakeConnect(ws),
        )
        await feed.run(stop)
        assert writer.rows == []

    with caplog.at_level(logging.WARNING, logger="reference"):
        asyncio.run(scenario())
    assert caplog.records == []
```

- [ ] **Step 7: Run malformed/irrelevant isolation tests and record RED**

Run:

```bash
python3 -m pytest tests/test_reference.py -k 'malformed_relevant or irrelevant_frames' -q
```

Expected RED: the uncaught `ReferenceParseError` from the first malformed
relevant frame ends the feed before the following valid frame can be written.

- [ ] **Step 8: Complete malformed and irrelevant frame handling**

Wrap `json.loads` and each pure parser call exactly as follows, while keeping
`recv_ms` capture as the first loop-body statement:

```python
try:
    msg = json.loads(raw)
except (json.JSONDecodeError, TypeError) as exc:
    log.warning("[%s] malformed reference JSON: %s", self.name, exc)
    continue
try:
    parsed = parse_hl_reference(msg, coin=self.coin)
except ReferenceParseError as exc:
    log.warning(
        "[%s] malformed relevant reference frame: %s",
        self.name,
        exc,
    )
    continue
```

Use `parse_lighter_reference` in the Lighter variant. Make the tests GREEN
without importing `OrderBook`, `Engine`, or anything from
`entropy_arb.feeds`. Do not accept a callback parameter. Keep all reference
logging under the `reference` logger.

- [ ] **Step 9: Run malformed/irrelevant tests GREEN**

Run the Step 7 command again.

Expected GREEN: malformed relevant data is warned/skipped, irrelevant data is
silent, the connection continues, and zero selected tests fail.

- [ ] **Step 10: Add RED deterministic reconnect/backoff test**

Add a fake connection context that always fails:

```python
def test_reference_feed_reconnect_backoff_caps_at_thirty_seconds():
    class FailedConnection:
        async def __aenter__(self):
            raise OSError("disconnected")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class AlwaysFailConnect:
        def __call__(self, url, **kwargs):
            return FailedConnection()

    async def scenario():
        stop = asyncio.Event()
        delays = []

        async def fake_sleep(delay):
            delays.append(delay)
            if len(delays) == 7:
                stop.set()

        feed = LighterReferenceFeed(
            "LIGHTER",
            "wss://example.invalid/ws",
            139,
            StubReferenceWriter(),
            connect=AlwaysFailConnect(),
            sleep=fake_sleep,
        )
        await feed.run(stop)
        assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]

    asyncio.run(scenario())


def test_reference_feed_reconnect_backoff_resets_after_successful_connection():
    class FailedConnection:
        async def __aenter__(self):
            raise OSError("disconnected")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class OneFrameThenDrop:
        def __init__(self):
            self.sent = []
            self.yielded = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.yielded:
                self.yielded = True
                return json.dumps({"type": "connected"})
            raise OSError("dropped after connect")

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    class ConnectSequence:
        def __init__(self):
            self.attempt = 0

        def __call__(self, url, **kwargs):
            self.attempt += 1
            if self.attempt == 1:
                return FailedConnection()
            return OneFrameThenDrop()

    async def scenario():
        stop = asyncio.Event()
        delays = []

        async def fake_sleep(delay):
            delays.append(delay)
            if len(delays) == 2:
                stop.set()

        feed = LighterReferenceFeed(
            "LIGHTER",
            "wss://example.invalid/ws",
            139,
            StubReferenceWriter(),
            connect=ConnectSequence(),
            sleep=fake_sleep,
        )
        await feed.run(stop)
        assert delays == [1.0, 1.0]

    asyncio.run(scenario())
```

- [ ] **Step 11: Run reconnect test and record RED**

Run:

```bash
python3 -m pytest tests/test_reference.py -k 'reference_feed_reconnect_backoff' -q
```

Expected RED: backoff is absent, does not match the capped sequence, or does
not reset to one second after a successful connection.

- [ ] **Step 12: Implement non-fatal reconnect loops**

For both feeds:

1. Start at `1.0` seconds.
2. Catch connection/transport/parser-wrapper errors except
   `asyncio.CancelledError`.
3. Log one warning for the failed connection attempt.
4. If stop is not set, await the injected sleep.
5. Double the delay and cap at `30.0`.
6. Reset to `1.0` after a successful connection.
7. Never touch a trading book's readiness or callback.

- [ ] **Step 13: Run all Task 1-3 tests GREEN**

Run:

```bash
python3 -m pytest tests/test_reference.py -q
```

Expected GREEN: every reference writer, parser, feed, ordering, isolation, and
backoff test passes; zero failures and no real network access.

- [ ] **Step 14: Commit Task 3**

```bash
git add entropy_arb/reference.py tests/test_reference.py
git commit -m "feat: collect public reference feeds"
```

---

### Task 4: ReferenceRecorder Coordinator and Shutdown

**Files:**
- Modify: `entropy_arb/reference.py`
- Modify: `tests/test_reference.py`

**Interfaces:**
- Consumes Task 1 writers and Task 3 feed classes.
- Produces:

```python
REFERENCE_FEED_STOP_TIMEOUT_SEC = 5.0


class ReferenceRecorder:
    def __init__(
        self,
        *,
        symbol: str,
        hedge_key: str,
        entropy_ws_url: str,
        entropy_coin: str,
        hedge_ws_url: str,
        hedge_market_id: int,
        directory: str = "logs",
        feed_stop_timeout_sec: float = REFERENCE_FEED_STOP_TIMEOUT_SEC,
    ) -> None:
        entropy_path, hedge_path = reference_paths(
            symbol,
            hedge_key,
            directory,
        )
        self.entropy_writer = ReferenceCsvWriter(
            entropy_path,
            ENTROPY_REFERENCE_HEADER,
        )
        self.hedge_writer = ReferenceCsvWriter(
            hedge_path,
            LIGHTER_REFERENCE_HEADER,
        )
        self.entropy_feed = HLReferenceFeed(
            "entropy-reference",
            entropy_ws_url,
            entropy_coin,
            self.entropy_writer,
        )
        self.hedge_feed = LighterReferenceFeed(
            "hedge-reference",
            hedge_ws_url,
            hedge_market_id,
            self.hedge_writer,
        )
        self.feed_stop_timeout_sec = feed_stop_timeout_sec

    async def run(self, stop: asyncio.Event) -> None:
        feed_stop = asyncio.Event()
        feed_tasks = [
            asyncio.create_task(
                self.entropy_feed.run(feed_stop),
                name="reference-entropy",
            ),
            asyncio.create_task(
                self.hedge_feed.run(feed_stop),
                name="reference-hedge",
            ),
        ]
        try:
            await stop.wait()
        finally:
            self.entropy_writer.stop_accepting()
            self.hedge_writer.stop_accepting()
            feed_stop.set()
            _, pending = await asyncio.wait(
                feed_tasks,
                timeout=self.feed_stop_timeout_sec,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*feed_tasks, return_exceptions=True)
            self.entropy_writer.close()
            self.hedge_writer.close()
```

- Task 5 constructs this only after runtime market metadata is resolved.
- `feed_stop_timeout_sec` is an internal lifecycle bound and test seam, not a
  config or CLI option.

- [ ] **Step 1: Add RED test for final buffered rows from both writers**

Extend the reference import with `ReferenceRecorder`, then patch feed classes
with deterministic writers:

```python
def test_reference_recorder_shutdown_flushes_both_final_buffers(
        tmp_path, monkeypatch):
    async def scenario():
        from entropy_arb import reference

        feeds_started = 0
        both_started = asyncio.Event()

        class EntropyFeed:
            def __init__(self, name, ws_url, coin, writer, **kwargs):
                self.writer = writer

            async def run(self, stop):
                nonlocal feeds_started
                self.writer.write((1, 100.0, 101.0))
                feeds_started += 1
                if feeds_started == 2:
                    both_started.set()
                await stop.wait()

        class HedgeFeed:
            def __init__(self, name, ws_url, market_id, writer, **kwargs):
                self.writer = writer

            async def run(self, stop):
                nonlocal feeds_started
                self.writer.write((1, 2, 100.0, 101.0))
                feeds_started += 1
                if feeds_started == 2:
                    both_started.set()
                await stop.wait()

        monkeypatch.setattr(reference, "HLReferenceFeed", EntropyFeed)
        monkeypatch.setattr(reference, "LighterReferenceFeed", HedgeFeed)
        recorder = ReferenceRecorder(
            symbol="SNDK",
            hedge_key="lighter",
            entropy_ws_url="wss://hl.invalid/ws",
            entropy_coin="io:SNDK",
            hedge_ws_url="wss://lighter.invalid/ws",
            hedge_market_id=139,
            directory=str(tmp_path),
        )
        stop = asyncio.Event()
        task = asyncio.create_task(recorder.run(stop))
        await both_started.wait()
        stop.set()
        await task
        entropy_path, hedge_path = reference_paths(
            "SNDK", "lighter", str(tmp_path)
        )
        assert len(read_rows(entropy_path)) == 2
        assert len(read_rows(hedge_path)) == 2

    asyncio.run(scenario())
```

- [ ] **Step 2: Run final-buffer test and record RED**

Run:

```bash
python3 -m pytest tests/test_reference.py::test_reference_recorder_shutdown_flushes_both_final_buffers -q
```

Expected RED: import fails because `ReferenceRecorder` is absent.

- [ ] **Step 3: Implement coordinator construction and a final-buffer baseline**

In `__init__`:

1. Call `reference_paths(symbol, hedge_key, directory)`.
2. Create the Entropy writer with `ENTROPY_REFERENCE_HEADER`.
3. Create the hedge writer with `LIGHTER_REFERENCE_HEADER`.
4. Create `HLReferenceFeed` and `LighterReferenceFeed` with only public runtime
   metadata and their respective writers.

In `run`, create a private feed stop event and exactly two feed tasks. For this
first GREEN, stop accepting and close the writers after the outer stop signal,
then signal/cancel/await feeds. This deliberately proves the final-buffer test
without yet satisfying the required feed-before-close ordering:

```python
async def run(self, stop: asyncio.Event) -> None:
    feed_stop = asyncio.Event()
    tasks = [
        asyncio.create_task(
            self.entropy_feed.run(feed_stop),
            name="reference-entropy",
        ),
        asyncio.create_task(
            self.hedge_feed.run(feed_stop),
            name="reference-hedge",
        ),
    ]
    try:
        await stop.wait()
    finally:
        self.entropy_writer.stop_accepting()
        self.hedge_writer.stop_accepting()
        self.entropy_writer.close()
        self.hedge_writer.close()
        feed_stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
```

Do not commit this intermediate order. Steps 5-7 replace it with the approved
race-free shutdown contract before Task 4 is committed.

- [ ] **Step 4: Run final-buffer test GREEN**

Run the Step 2 command again.

Expected GREEN: both files contain one header and one final row despite neither
writer reaching 10 rows.

- [ ] **Step 5: Add RED shutdown-order, forced-cancel, and sibling-isolation tests**

Add ordered spies:

```python
def test_shutdown_stops_feeds_before_idempotent_writer_close(monkeypatch):
    async def scenario():
        from entropy_arb import reference

        events = []
        feed_count = 0
        feeds_started = asyncio.Event()

        class OrderedWriter:
            enabled = True

            def __init__(self, path, header, flush_rows=10):
                self.path = path
                self.closed = False

            def write(self, row):
                assert not self.closed

            def stop_accepting(self):
                events.append(f"stop-accepting:{self.path}")

            def close(self):
                if not self.closed:
                    assert events.count("feed-stopped") == 2
                    events.append(f"close:{self.path}")
                    self.closed = True

        class OrderedFeed:
            def __init__(self, name, ws_url, identity, writer, **kwargs):
                self.writer = writer

            async def run(self, stop):
                nonlocal feed_count
                feed_count += 1
                if feed_count == 2:
                    feeds_started.set()
                await stop.wait()
                events.append("feed-stopped")

        monkeypatch.setattr(reference, "ReferenceCsvWriter", OrderedWriter)
        monkeypatch.setattr(reference, "HLReferenceFeed", OrderedFeed)
        monkeypatch.setattr(reference, "LighterReferenceFeed", OrderedFeed)
        recorder = ReferenceRecorder(
            symbol="SNDK",
            hedge_key="lighter-rh",
            entropy_ws_url="wss://hl.invalid/ws",
            entropy_coin="io:SNDK",
            hedge_ws_url="wss://rh.invalid/ws",
            hedge_market_id=32,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(recorder.run(stop))
        await feeds_started.wait()
        stop.set()
        await task
        first_close = min(
            index for index, event in enumerate(events)
            if event.startswith("close:")
        )
        last_feed_stop = max(
            index for index, event in enumerate(events)
            if event == "feed-stopped"
        )
        assert last_feed_stop < first_close
        recorder.entropy_writer.close()
        recorder.hedge_writer.close()

    asyncio.run(scenario())


def test_stuck_feed_is_cancelled_and_awaited_before_close(monkeypatch):
    async def scenario():
        from entropy_arb import reference

        events = []
        started_count = 0
        both_started = asyncio.Event()

        class OrderedWriter:
            def __init__(self, path, header, flush_rows=10):
                self.path = path
                self.enabled = True
                self.closed = False

            def write(self, row):
                if self.closed:
                    raise AssertionError("write after close")

            def stop_accepting(self):
                self.enabled = False

            def close(self):
                if not self.closed:
                    self.closed = True
                    events.append(f"close:{self.path}")

        class StuckFeed:
            def __init__(self, name, ws_url, identity, writer, **kwargs):
                self.name = name

            async def run(self, stop):
                nonlocal started_count
                started_count += 1
                if started_count == 2:
                    both_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    events.append(f"feed-cancelled:{self.name}")
                    raise

        monkeypatch.setattr(reference, "ReferenceCsvWriter", OrderedWriter)
        monkeypatch.setattr(reference, "HLReferenceFeed", StuckFeed)
        monkeypatch.setattr(reference, "LighterReferenceFeed", StuckFeed)
        recorder = ReferenceRecorder(
            symbol="SNDK",
            hedge_key="lighter",
            entropy_ws_url="wss://hl.invalid/ws",
            entropy_coin="io:SNDK",
            hedge_ws_url="wss://lighter.invalid/ws",
            hedge_market_id=139,
            feed_stop_timeout_sec=0.01,
        )
        stop = asyncio.Event()
        task = asyncio.create_task(recorder.run(stop))
        await both_started.wait()
        stop.set()
        await task

        cancel_positions = [
            index for index, event in enumerate(events)
            if event.startswith("feed-cancelled:")
        ]
        close_positions = [
            index for index, event in enumerate(events)
            if event.startswith("close:")
        ]
        assert len(cancel_positions) == 2
        assert len(close_positions) == 2
        assert max(cancel_positions) < min(close_positions)

    asyncio.run(scenario())
```

Add three more deterministic cases in this same RED batch with names selected
by the focused commands below:

1. `test_feed_failure_preserves_sibling_writer`: Entropy feed raises
   `RuntimeError("feed failed")` while the hedge feed writes and waits for stop;
   assert the hedge row is flushed.
2. `test_bad_header_disables_only_one_sibling_writer`: pre-create a bad Entropy
   header, construct the coordinator, and let both feeds attempt writes; assert
   Entropy bytes are unchanged and the valid hedge file contains its row.
3. `test_sibling_isolation_prevents_write_after_stop_accepting`: assert no
   write is accepted after `stop_accepting()` and no feed calls `write()` after
   the close events.

- [ ] **Step 6: Run shutdown-order tests and record RED**

Run:

```bash
python3 -m pytest tests/test_reference.py -k 'shutdown_stops or stuck_feed or sibling' -q
```

Expected RED: the Step 3 baseline closes writers before feed termination, so
the ordered spies observe close-before-stop/cancellation even if sibling cases
already pass.

- [ ] **Step 7: Make ordered shutdown GREEN**

Replace the Step 3 baseline with the final interface skeleton at the start of
Task 4: stop accepting, signal `feed_stop`, await up to
`feed_stop_timeout_sec`, cancel only pending tasks, await every task result,
then close both writers. Preserve both writer closes even when one feed
returned early or raised. `asyncio.gather(*feed_tasks,
return_exceptions=True)` must consume both feed results without propagating them
into Engine.

- [ ] **Step 8: Complete sibling isolation without changing shutdown order**

If a sibling-isolation assertion from Step 5 is still RED, contain that failure
at its owner: feeds catch transport/parser failures, writers disable locally,
and the coordinator consumes task results independently. Do not make either
writer or feed a readiness gate for the other.

- [ ] **Step 9: Run all coordinator tests GREEN**

Run:

```bash
python3 -m pytest tests/test_reference.py -k 'reference_recorder or shutdown or stuck_feed or sibling' -q
```

Expected GREEN: final buffers, no-write-after-close ordering, forced
cancellation, and sibling isolation all pass with zero failures.

- [ ] **Step 10: Run the complete reference suite GREEN**

Run:

```bash
python3 -m pytest tests/test_reference.py -q
```

Expected GREEN: all Task 1-4 tests pass; no real network calls and zero
failures.

- [ ] **Step 11: Commit Task 4**

```bash
git add entropy_arb/reference.py tests/test_reference.py
git commit -m "feat: coordinate reference recorder shutdown"
```

---

### Task 5: Engine Lifecycle Wiring

**Files:**
- Modify: `entropy_arb/engine.py:27-31,43-55,139-224`
- Modify: `tests/test_engine.py:19-59` and append lifecycle tests after current
  signal tests

**Interfaces:**
- Consumes `ReferenceRecorder` from Task 4 and runtime venue metadata:
  - `self.entropy.ws_url`
  - `self.entropy.coin`
  - `self.hedge.profile.ws_url`
  - `self.hedge.market_id`
- Produces:

```python
REFERENCE_HEDGE_KEYS = frozenset(("lighter", "lighter-rh"))


def _build_reference_recorder(self) -> Optional[ReferenceRecorder]:
    """Build only for an enabled supported Lighter hedge."""


async def _run_reference(self) -> None:
    """Contain an unexpected reference coordinator failure."""
```

- `self.reference: Optional[ReferenceRecorder]` stores telemetry lifecycle state
  only. Strategy methods never read it.

- [ ] **Step 1: Extend test config/stubs without changing threshold assertions**

Change `make_cfg` only enough to accept `hedge_venue` and
`recorder_enabled`:

```python
def make_cfg(
    midline=5.0,
    upper=4.0,
    lower=3.0,
    *,
    hedge_venue="lighter-rh",
    recorder_enabled=True,
):
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(f"""
thresholds:
  midline_bps: {midline}
  upper_bps: {upper}
  lower_bps: {lower}
execution:
  premium_persist_sec: 0.0
recorder:
  enabled: {str(recorder_enabled).lower()}
  csv: {os.path.join(tempfile.gettempdir(), "engine-minutes.csv")}
""")
    f.close()
    return load_config(
        f.name,
        NO_ENV,
        symbol="SNDK",
        hedge_venue=hedge_venue,
    )
```

Do not edit existing threshold, inventory, or scan test bodies.

- [ ] **Step 2: Add RED lifecycle selection and runtime-metadata tests**

Add a helper that attaches runtime metadata without calling network methods:

```python
from types import SimpleNamespace


def attach_reference_venues(
    eng,
    *,
    market_id=32,
    hedge_ws_url="wss://api.rh.lighter.xyz/stream",
):
    eng.entropy = StubVenue("entropy", "ENTROPY")
    eng.entropy.ws_url = "wss://api.hyperliquid.xyz/ws"
    eng.entropy.coin = "io:SNDK"
    eng.hedge = StubVenue("hedge", "RH")
    eng.hedge.kind = "lighter"
    eng.hedge.profile = SimpleNamespace(ws_url=hedge_ws_url)
    eng.hedge.market_id = market_id


@pytest.mark.parametrize(
    ("record_only", "recorder_enabled", "expected"),
    [
        (True, False, True),
        (False, True, True),
        (False, False, False),
    ],
)
def test_reference_lifecycle_matrix(
        monkeypatch, record_only, recorder_enabled, expected):
    from entropy_arb import engine as engine_module

    captured = []

    class SpyReferenceRecorder:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(
        engine_module, "ReferenceRecorder", SpyReferenceRecorder
    )
    eng = Engine(
        make_cfg(recorder_enabled=recorder_enabled),
        record_only=record_only,
    )
    attach_reference_venues(eng)
    recorder = eng._build_reference_recorder()
    assert (recorder is not None) is expected
    if expected:
        assert captured == [{
            "symbol": "SNDK",
            "hedge_key": "lighter-rh",
            "entropy_ws_url": "wss://api.hyperliquid.xyz/ws",
            "entropy_coin": "io:SNDK",
            "hedge_ws_url": "wss://api.rh.lighter.xyz/stream",
            "hedge_market_id": 32,
        }]


@pytest.mark.parametrize(
    ("hedge_key", "hedge_ws_url", "market_id"),
    [
        (
            "lighter",
            "wss://mainnet.zklighter.elliot.ai/stream",
            139,
        ),
        (
            "lighter-rh",
            "wss://api.rh.lighter.xyz/stream",
            32,
        ),
    ],
)
def test_reference_factory_uses_runtime_resolved_metadata(
        monkeypatch, hedge_key, hedge_ws_url, market_id):
    from entropy_arb import engine as engine_module

    captured = []

    class SpyReferenceRecorder:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr(
        engine_module, "ReferenceRecorder", SpyReferenceRecorder
    )
    eng = Engine(
        make_cfg(hedge_venue=hedge_key, recorder_enabled=True)
    )
    attach_reference_venues(
        eng,
        market_id=market_id,
        hedge_ws_url=hedge_ws_url,
    )
    eng._build_reference_recorder()
    assert captured[0]["entropy_coin"] == "io:SNDK"
    assert captured[0]["hedge_key"] == hedge_key
    assert captured[0]["hedge_ws_url"] == hedge_ws_url
    assert captured[0]["hedge_market_id"] == market_id


def test_tradexyz_does_not_build_reference_collector():
    eng = Engine(
        make_cfg(hedge_venue="tradexyz", recorder_enabled=True),
        record_only=True,
    )
    eng.entropy = SimpleNamespace(
        ws_url="wss://api.hyperliquid.xyz/ws",
        coin="io:SNDK",
    )
    eng.hedge = SimpleNamespace(kind="hl")
    assert eng._build_reference_recorder() is None
```

Add `import pytest` to `tests/test_engine.py`; it is already a project test
dependency and does not modify runtime requirements.

- [ ] **Step 3: Run lifecycle selection tests and record RED**

Run:

```bash
python3 -m pytest tests/test_engine.py -k 'reference_lifecycle or reference_factory or tradexyz' -q
```

Expected RED: `Engine` has no `_build_reference_recorder` and no imported
`ReferenceRecorder`.

- [ ] **Step 4: Implement the reference factory without adapter/config changes**

In `Engine.__init__` add:

```python
self.reference: Optional[ReferenceRecorder] = None
```

Add:

```python
def _build_reference_recorder(self) -> Optional[ReferenceRecorder]:
    if self.cfg.hedge_venue not in REFERENCE_HEDGE_KEYS:
        return None
    if not (self.record_only or self.cfg.recorder_enabled):
        return None
    return ReferenceRecorder(
        symbol=self.cfg.symbol,
        hedge_key=self.cfg.hedge_venue,
        entropy_ws_url=self.entropy.ws_url,
        entropy_coin=self.entropy.coin,
        hedge_ws_url=self.hedge.profile.ws_url,
        hedge_market_id=self.hedge.market_id,
    )
```

Do not add `_run_reference()` yet; Step 6 must first prove the non-fatal wrapper
and event isolation are RED.

- [ ] **Step 5: Run lifecycle selection tests GREEN**

Run the Step 3 command again.

Expected GREEN: all three lifecycle rows and the `tradexyz` exclusion pass.

- [ ] **Step 6: Add RED tests for no strategy wakeup and non-fatal failure**

Add:

```python
def test_reference_factory_has_no_strategy_wakeup_dependency(monkeypatch):
    from entropy_arb import engine as engine_module

    captured = {}

    class SpyReferenceRecorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        engine_module, "ReferenceRecorder", SpyReferenceRecorder
    )
    eng = Engine(make_cfg(), record_only=True)
    attach_reference_venues(eng)
    eng.reference = eng._build_reference_recorder()
    assert "notify" not in captured
    assert "update_evt" not in captured
    assert not eng._update_evt.is_set()


def test_reference_failure_is_nonfatal_and_does_not_set_engine_events(caplog):
    class BrokenReference:
        async def run(self, stop):
            raise RuntimeError("reference failed")

    async def scenario():
        eng = Engine(make_cfg(), record_only=True)
        eng.reference = BrokenReference()
        await eng._run_reference()
        assert not eng.stop.is_set()
        assert not eng._update_evt.is_set()
        assert not eng._reconcile_evt.is_set()

    asyncio.run(scenario())
    assert "reference recorder failed" in caplog.text


def test_no_strategy_wakeup_after_successful_reference_run():
    class SuccessfulReference:
        async def run(self, stop):
            return None

    async def scenario():
        eng = Engine(make_cfg(), record_only=False)
        evaluations = 0

        async def tracked_evaluate():
            nonlocal evaluations
            evaluations += 1

        eng._evaluate = tracked_evaluate
        eng.reference = SuccessfulReference()
        strategy_task = asyncio.create_task(eng._strategy_loop())
        await asyncio.sleep(0)
        await eng._run_reference()
        await asyncio.sleep(0)
        assert not eng._update_evt.is_set()
        assert evaluations == 0
        eng.stop.set()
        eng._update_evt.set()
        await strategy_task

    asyncio.run(scenario())
```

- [ ] **Step 7: Run no-wakeup/failure tests and record RED**

Run:

```bash
python3 -m pytest tests/test_engine.py -k 'no_strategy_wakeup or reference_failure' -q
```

Expected RED: reference wrapper/factory behavior is missing or an event is
incorrectly coupled.

- [ ] **Step 8: Keep reference traffic out of Engine events**

Make Step 6 GREEN solely through constructor arguments and this containment
wrapper:

```python
async def _run_reference(self) -> None:
    if self.reference is None:
        return
    try:
        await self.reference.run(self.stop)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("reference recorder failed")
```

The wrapper never sets `self.stop`, `self._update_evt`, or
`self._reconcile_evt`. Do not pass `self._update_evt.set`, an OrderBook, or a
strategy callback into `ReferenceRecorder`.

- [ ] **Step 9: Add RED integration test for graceful Engine ownership**

Use network-free lifecycle stubs:

```python
def test_engine_awaits_reference_shutdown_without_cancelling_it(monkeypatch):
    from entropy_arb import engine as engine_module

    async def scenario():
        started = asyncio.Event()
        closed = asyncio.Event()
        was_cancelled = False

        class LifecycleVenue:
            def __init__(self, kind, *, coin=None, market_id=None, ws_url=None):
                self.kind = kind
                self.key = "entropy" if coin else "hedge"
                self.name = self.key.upper()
                self.conf = SimpleNamespace(symbol="SNDK")
                self.ws_url = ws_url
                self.coin = coin
                self.market_id = market_id
                self.profile = SimpleNamespace(ws_url=ws_url)
                self.size_decimals = 4
                self.min_base = 0.0001
                self.min_quote = 10.0
                self.fee_bps = 0.0
                self.book = OrderBook()

            async def load_market(self):
                return None

            def start_tasks(self, stop, notify, live):
                return []

            async def close(self):
                return None

        class SpyReferenceRecorder:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def run(self, stop):
                nonlocal was_cancelled
                started.set()
                try:
                    await stop.wait()
                    await asyncio.sleep(0)
                    closed.set()
                except asyncio.CancelledError:
                    was_cancelled = True
                    raise

        class QuietMinuteRecorder:
            rows_written = 0

            def __init__(self, *args, **kwargs):
                return None

            async def run(self, stop):
                await stop.wait()

        cfg = make_cfg(recorder_enabled=False)
        eng = Engine(cfg, record_only=True)
        venues = iter([
            LifecycleVenue(
                "hl",
                coin="io:SNDK",
                ws_url="wss://api.hyperliquid.xyz/ws",
            ),
            LifecycleVenue(
                "lighter",
                market_id=32,
                ws_url="wss://api.rh.lighter.xyz/stream",
            ),
        ])
        monkeypatch.setattr(eng, "_make_venue", lambda conf: next(venues))
        monkeypatch.setattr(
            engine_module, "ReferenceRecorder", SpyReferenceRecorder
        )
        monkeypatch.setattr(
            engine_module, "MinuteRecorder", QuietMinuteRecorder
        )
        run_task = asyncio.create_task(eng._run_inner())
        await asyncio.wait_for(started.wait(), timeout=0.2)
        eng.request_stop()
        await asyncio.wait_for(run_task, timeout=1.0)
        assert closed.is_set()
        assert was_cancelled is False

    asyncio.run(scenario())
```

- [ ] **Step 10: Run Engine ownership integration test and record RED**

Run:

```bash
python3 -m pytest tests/test_engine.py::test_engine_awaits_reference_shutdown_without_cancelling_it -q
```

Expected RED: current generic task handling cancels or never starts the
reference coordinator.

- [ ] **Step 11: Wire and separately await the reference task**

After both `load_market()` calls have completed, live credential/signer checks
have succeeded when applicable, and normal startup reconciliation is complete,
construct/start the reference task immediately before the existing generic
task list:

```python
reference_task = None
self.reference = self._build_reference_recorder()
if self.reference is not None:
    reference_task = asyncio.create_task(
        self._run_reference(),
        name="reference",
    )
```

Do not append this task to the generic `tasks` list. At shutdown:

1. `self.stop` is already set by `request_stop()`.
2. Preserve the current in-flight execution settlement wait.
3. Cancel/gather the existing generic tasks exactly as today.
4. Await `reference_task` with
   `asyncio.gather(reference_task, return_exceptions=True)`.
5. Only then close venues and return.

ReferenceRecorder owns its internal feed cancellation and writer close. Engine
must never directly close reference writers or cancel `reference_task`.

- [ ] **Step 12: Run all new Engine lifecycle tests GREEN**

Run:

```bash
python3 -m pytest tests/test_engine.py -k 'reference or tradexyz' -q
```

Expected GREEN: lifecycle matrix, runtime metadata, no-wakeup, non-fatal
failure, `tradexyz` exclusion, and graceful ownership all pass.

- [ ] **Step 13: Run all Engine and reference tests GREEN**

Run:

```bash
python3 -m pytest tests/test_reference.py tests/test_engine.py -q
```

Expected GREEN: all selected tests pass with zero failures; existing threshold,
inventory, and scan tests remain unchanged and green.

- [ ] **Step 14: Review the Engine diff before committing**

Run:

```bash
git diff -- entropy_arb/engine.py tests/test_engine.py
```

Confirm every `engine.py` hunk is limited to:

- Import/type state for `ReferenceRecorder`.
- Supported-hedge lifecycle selection.
- Runtime metadata construction after `load_market()`.
- Separate reference task start/await.

There must be no hunks in `_inv_add_bps`, `_eff_threshold`, `_plan`,
`_strategy_loop`, `_evaluate`, `_scan`, `_execute`, sizing, reconciliation,
or emergency hedging.

- [ ] **Step 15: Commit Task 5**

```bash
git add entropy_arb/engine.py tests/test_engine.py
git commit -m "feat: wire reference recorder lifecycle"
```

---

### Task 6: Regression Gates and Credential-Free Public Smoke

**Files:**
- Verify: `entropy_arb/reference.py`
- Verify: `entropy_arb/engine.py`
- Verify: `tests/test_reference.py`
- Verify: `tests/test_engine.py`
- Verify unchanged: `entropy_arb/recorder.py`
- Verify unchanged: `entropy_arb/feeds.py`
- Verify unchanged: `entropy_arb/config.py`
- Verify unchanged: `entropy_arb/venue_hl.py`
- Verify unchanged: `entropy_arb/venue_lighter.py`
- Verify unchanged: `tests/test_recorder.py`

**Interfaces:**
- Consumes the complete implementation from Tasks 1-5.
- Produces verification evidence only; no new production interface.

- [ ] **Step 1: Run the focused reference suite**

Run:

```bash
python3 -m pytest tests/test_reference.py -q
```

Expected: all collected tests pass; zero failures.

- [ ] **Step 2: Run the focused Engine suite**

Run:

```bash
python3 -m pytest tests/test_engine.py -q
```

Expected: all lifecycle and pre-existing signal tests pass; zero failures.

- [ ] **Step 3: Run the unchanged recorder suite**

Run:

```bash
python3 -m pytest tests/test_recorder.py -q
```

Expected: all minute and samples-v2 tests pass; zero failures.

- [ ] **Step 4: Run the full deterministic suite**

Run:

```bash
python3 -m pytest -q
```

Expected: all collected tests pass with zero failures. Do not invent or
pre-state a test count; record the actual count.

- [ ] **Step 5: Run whitespace and forbidden-file diff gates**

Run:

```bash
git diff --check
git diff --exit-code 1a53aba6282293dad803f9cc5bd801e0d7738a1e -- entropy_arb/recorder.py entropy_arb/feeds.py entropy_arb/config.py entropy_arb/venue_hl.py entropy_arb/venue_lighter.py tests/test_recorder.py
git diff 1a53aba6282293dad803f9cc5bd801e0d7738a1e -- entropy_arb/engine.py
```

Expected:

- `git diff --check` has no output.
- The `--exit-code` command exits 0 with no output.
- Manual Engine review shows only lifecycle wiring and no signal, threshold,
  sizing, execution, reconciliation, or emergency-hedge changes.

- [ ] **Step 6: Inspect existing recorder processes before any smoke**

Run:

```bash
pgrep -af 'main.py.*record-only' || true
```

Record every PID and command. Never stop an existing recorder automatically.
Use the isolated working directories below whether or not another recorder is
running, so repo `logs/` and historical files cannot collide.

- [ ] **Step 7: Create two isolated smoke working directories**

Run:

```bash
LIGHTER_SMOKE_DIR=$(mktemp -d /tmp/entropy-reference-lighter.XXXXXX)
RH_SMOKE_DIR=$(mktemp -d /tmp/entropy-reference-rh.XXXXXX)
test -d "$LIGHTER_SMOKE_DIR"
test -d "$RH_SMOKE_DIR"
```

These directories isolate relative `logs/` paths while executing the source
from the repository. They do not create a second recorder against an existing
process's files.

- [ ] **Step 8: Run the Lighter smoke in the isolated directory**

Run from `$LIGHTER_SMOKE_DIR`:

```bash
cd "$LIGHTER_SMOKE_DIR"
PYTHONDONTWRITEBYTECODE=1 python3 /Users/liaoyuchen/entropy-arb/main.py --config /Users/liaoyuchen/entropy-arb/config.yaml --env-file "$LIGHTER_SMOKE_DIR/no-such.env" --record-only --symbol SNDK --hedge lighter --no-dashboard
```

Allow 15-20 seconds, then send one Ctrl+C. Expected: no credential prompt, no
order submission, clean graceful shutdown, and exactly these two reference
files under the isolated `logs/`:

```text
logs/reference-SNDK-lighter-entropy.csv
logs/reference-SNDK-lighter.csv
```

- [ ] **Step 9: Validate Lighter files and rows**

Run:

```bash
find "$LIGHTER_SMOKE_DIR/logs" -maxdepth 1 -type f -name 'reference-*.csv' -print | sort
head -n 2 "$LIGHTER_SMOKE_DIR/logs/reference-SNDK-lighter-entropy.csv"
head -n 2 "$LIGHTER_SMOKE_DIR/logs/reference-SNDK-lighter.csv"
wc -l "$LIGHTER_SMOKE_DIR/logs/reference-SNDK-lighter-entropy.csv" "$LIGHTER_SMOKE_DIR/logs/reference-SNDK-lighter.csv"
```

Expected:

- `find` prints exactly the two paths listed in Step 8.
- Headers are exactly the approved Entropy and Lighter schemas.
- Each file contains at least one positive-valued data row.
- Graceful Ctrl+C exposes the final rows even when the buffer has fewer than
  10 rows.

- [ ] **Step 10: Run and validate the Lighter-RH smoke separately**

Run from `$RH_SMOKE_DIR`:

```bash
cd "$RH_SMOKE_DIR"
PYTHONDONTWRITEBYTECODE=1 python3 /Users/liaoyuchen/entropy-arb/main.py --config /Users/liaoyuchen/entropy-arb/config.yaml --env-file "$RH_SMOKE_DIR/no-such.env" --record-only --symbol SNDK --hedge lighter-rh --no-dashboard
```

Allow 15-20 seconds, send one Ctrl+C, then run:

```bash
find "$RH_SMOKE_DIR/logs" -maxdepth 1 -type f -name 'reference-*.csv' -print | sort
head -n 2 "$RH_SMOKE_DIR/logs/reference-SNDK-lighter-rh-entropy.csv"
head -n 2 "$RH_SMOKE_DIR/logs/reference-SNDK-lighter-rh.csv"
wc -l "$RH_SMOKE_DIR/logs/reference-SNDK-lighter-rh-entropy.csv" "$RH_SMOKE_DIR/logs/reference-SNDK-lighter-rh.csv"
```

Expected: exactly the two RH-namespaced files, exact headers, at least one
positive-valued data row per file, and final buffered rows visible after
graceful shutdown.

- [ ] **Step 11: Confirm running recorders and repo files were untouched**

Return to the repository and run:

```bash
cd /Users/liaoyuchen/entropy-arb
pgrep -af 'main.py.*record-only' || true
git status --short --branch
```

Expected:

- Every recorder PID observed in Step 6 is still present unless it exited
  independently.
- No smoke file exists under repository `logs/` due to this verification.
- Working-tree changes are limited to the implementation/test files from Tasks
  1-5.

- [ ] **Step 12: Review reference logs for isolation evidence**

Inspect both smoke command outputs. Confirm reconnect/parser/writer warnings, if
any, did not stop MinuteRecorder and did not produce an order attempt. If a
public endpoint is unavailable, report the smoke as blocked with the exact
public-feed error; do not change strategy, execution, credentials, or retry
policy to force a pass.

- [ ] **Step 13: Finish without an empty verification commit**

Task 5 is the final implementation commit when all Task 6 checks pass without
code changes. If a deterministic gate reveals a defect, return to the owning
task, add a focused failing regression test, implement the minimal correction,
rerun that task and Task 6, and commit:

```bash
git add entropy_arb/reference.py entropy_arb/engine.py tests/test_reference.py tests/test_engine.py
git commit -m "fix: preserve reference collector contracts"
```

Do not create a commit when verification produces no file changes.

## Final Implementation Review Checklist

- [ ] Exactly two process-namespaced files exist for each enabled supported
  hedge in healthy operation.
- [ ] Entropy and Lighter/RH headers exactly match the spec.
- [ ] Unchanged valid frames create independent rows.
- [ ] Ten-row flush and final close flush are both demonstrated.
- [ ] Restart append creates no duplicate header.
- [ ] Bad headers remain byte-identical and create no `.old` file.
- [ ] Feed/parser/writer failures remain local and non-fatal.
- [ ] Feed tasks are stopped/awaited before idempotent writer close.
- [ ] No write-after-close path exists.
- [ ] `recv_ms` is captured before JSON parsing.
- [ ] `server_ms` is positive-integer validated without wall-clock comparison.
- [ ] Reference constructors have no OrderBook, notify, or Engine event input.
- [ ] Reference traffic never sets `Engine._update_evt` or wakes strategy.
- [ ] `tradexyz` has no reference collector and existing behavior is unchanged.
- [ ] MinuteRecorder and samples-v2 files/code are unchanged.
- [ ] Threshold, strategy, sizing, and execution code is unchanged.
- [ ] `--record-only` smoke requires no credentials and submits no orders.
- [ ] No reference REST polling exists.
- [ ] Full pytest and `git diff --check` pass.
