# Market History Storage v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace scattered market-history CSV persistence with one multi-process-safe SQLite WAL database that stores 1 Hz samples, minute aggregates, and reference data, can non-destructively import legacy CSVs, and can produce one standalone snapshot for analysis.

**Architecture:** Add one `MarketHistoryStore` per entropy-arb process. Producers only append typed rows to process-local buffers; a background flush path performs batched SQLite transactions off the asyncio event loop, with WAL/busy timeout for cross-process contention. The live recorder and reference feeds share that store; migration and snapshot are separate command-line tools built on the same schema.

**Tech Stack:** Python 3 standard library only (`sqlite3`, `asyncio`, `threading`, `multiprocessing`, `csv`, `argparse`, `pathlib`), existing pytest suite. No new runtime dependency.

**Spec:** `docs/superpowers/specs/2026-09-01-market-history-storage-v1-design-zh-TW.md`

## Global Constraints

- Target branch: `feature/p2-persistence-research`.
- Approved baseline before documentation checkpoint: `dfd055ad020faa6371576c1977e2ecaa6e9cac6b`.
- Live source of truth becomes `data/market-history.sqlite`.
- SQLite runtime policy: `journal_mode=WAL`, `synchronous=FULL`, `busy_timeout=10000`, `foreign_keys=ON`.
- Preserve `calculate_premiums()` formulas and current ~1 Hz fresh-BBO sampling semantics exactly.
- Preserve current `_MinuteAgg` math and persisted numeric precision semantics; do not invent a new minute aggregation formula.
- Preserve reference parsers, market filtering, timestamps, Engine execution, hedging, reconcile, strategy observation, rate limits, outage handling, trade CSV, and engine logs.
- No dual-write market-history CSV compatibility mode after the live cutover.
- Legacy CSV migration is non-destructive and idempotent; conflicting keys never overwrite existing database rows.
- Do not interpolate or fill gaps.
- Market-history persistence does not contain strategy recommendations, regimes, optimizer output, or backtest results.
- No Market Analyzer, Parquet/DuckDB live store, storage daemon, server database, cloud sync, automatic backup scheduler, retention policy, trade-log migration, or L2 history.
- Runtime constants for v1: `FLUSH_INTERVAL_SEC = 10.0`, `MAX_PENDING_ROWS_PER_DATASET = 100_000`, production `busy_timeout_ms = 10_000`.
- SQLite work that may wait on another writer must run off the asyncio event loop. Producer `append_*()` calls must remain memory-only and fast.

---

## Pre-execution documentation checkpoint

Before Task 1, place the approved design and this plan at their exact repo paths and commit only those two files. Do not touch product code in this checkpoint.

```bash
git switch feature/p2-persistence-research
git status --short
git rev-parse HEAD
```

Expected before the docs commit: clean working tree and HEAD `dfd055ad020faa6371576c1977e2ecaa6e9cac6b`. If HEAD differs, stop and report the unexpected commits before continuing.

Copy the approved files to:

```text
docs/superpowers/specs/2026-09-01-market-history-storage-v1-design-zh-TW.md
docs/superpowers/plans/2026-09-01-market-history-storage-v1-implementation-plan-zh-TW.md
```

Then:

```bash
git add docs/superpowers/specs/2026-09-01-market-history-storage-v1-design-zh-TW.md \
        docs/superpowers/plans/2026-09-01-market-history-storage-v1-implementation-plan-zh-TW.md
git diff --cached --check
git commit -m "docs: design market history storage v1"
git push
```

Record the resulting docs commit SHA with:

```bash
git rev-parse HEAD | tee /tmp/entropy-arb-storage-v1-docs-commit
```

All implementation tasks start from that commit.

---

### Task 1: Core SQLite schema, typed rows, buffering, and conflict semantics

**Files:**
- Create: `entropy_arb/storage.py`
- Create: `tests/test_storage.py`

**Interfaces:**
- Produces: `SampleRow`, `MinuteRow`, `EntropyReferenceRow`, `HedgeReferenceRow` immutable row types.
- Produces: `InsertCounts(inserted, duplicates, conflicts)` and `FlushReport(ok, datasets)`.
- Produces: `MarketHistoryStore(path, *, busy_timeout_ms=10_000, max_pending_rows_per_dataset=100_000)`.
- Produces: memory-only `append_sample()`, `append_minute()`, `append_entropy_reference()`, `append_hedge_reference()`.
- Produces: synchronous `flush() -> FlushReport`, `import_rows(dataset, rows) -> InsertCounts`, `set_meta(key, value)`, `close()`.
- Later live code must call `flush()`/`close()` through `asyncio.to_thread`; `append_*()` stays on the event loop.

- [ ] **Step 1: Write schema/idempotency/conflict tests first**

Create `tests/test_storage.py` with the following initial tests. Use real temporary SQLite files, not mocks:

```python
import sqlite3
from pathlib import Path

from entropy_arb.storage import MarketHistoryStore, MinuteRow, SampleRow


def sample(ts: int = 1_700_000_000_000, premium: float = 10.0) -> SampleRow:
    return SampleRow(
        timestamp_ms=ts,
        symbol="SNDK",
        hedge="lighter-rh",
        premium_bps=premium,
        sell_edge_bps=8.0,
        buy_edge_bps=-12.0,
        entropy_bid=100.09,
        entropy_ask=100.11,
        hedge_bid=99.99,
        hedge_ask=100.01,
        entropy_book_update_ms=ts - 500,
        hedge_book_update_ms=ts - 300,
    )


def minute(ts: int = 1_699_999_980) -> MinuteRow:
    return MinuteRow(
        minute_ts=ts,
        symbol="SNDK",
        hedge="lighter-rh",
        entropy_bid=100.09,
        entropy_ask=100.11,
        hedge_bid=99.99,
        hedge_ask=100.01,
        premium_open_bps=10.0,
        premium_high_bps=20.0,
        premium_low_bps=10.0,
        premium_close_bps=20.0,
        premium_mean_bps=15.0,
        premium_std_bps=5.0,
        sell_edge_mean_bps=13.0,
        sell_edge_max_bps=18.0,
        buy_edge_mean_bps=-17.0,
        buy_edge_max_bps=-12.0,
        samples=2,
    )


def test_fresh_database_creates_schema_and_meta(tmp_path: Path):
    db = tmp_path / "market-history.sqlite"
    store = MarketHistoryStore(db)
    store.close()

    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"meta", "samples", "minutes", "entropy_reference", "hedge_reference"} <= tables
        assert conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone() == ("1",)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_second_open_is_idempotent(tmp_path: Path):
    db = tmp_path / "market-history.sqlite"
    MarketHistoryStore(db).close()
    MarketHistoryStore(db).close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_exact_duplicate_is_noop_and_conflicting_payload_does_not_replace(tmp_path: Path):
    db = tmp_path / "market-history.sqlite"
    store = MarketHistoryStore(db)
    store.append_sample(sample())
    first = store.flush()
    assert first.ok
    assert first.datasets["samples"].inserted == 1

    store.append_sample(sample())
    duplicate = store.flush()
    assert duplicate.datasets["samples"].duplicates == 1

    store.append_sample(sample(premium=999.0))
    conflict = store.flush()
    assert conflict.datasets["samples"].conflicts == 1
    store.close()

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT premium_bps FROM samples WHERE symbol=? AND hedge=? AND timestamp_ms=?",
            ("SNDK", "lighter-rh", 1_700_000_000_000),
        ).fetchone() == (10.0,)


def test_flush_transaction_rolls_back_all_datasets_and_keeps_buffers(tmp_path: Path):
    db = tmp_path / "market-history.sqlite"
    store = MarketHistoryStore(db)
    store._conn.execute(
        "CREATE TRIGGER fail_minutes BEFORE INSERT ON minutes "
        "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
    )
    store.append_sample(sample())
    store.append_minute(minute())

    report = store.flush()
    assert not report.ok
    assert store.pending_rows["samples"] == 1
    assert store.pending_rows["minutes"] == 1

    store._conn.execute("DROP TRIGGER fail_minutes")
    assert store.flush().ok
    store.close()

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM minutes").fetchone() == (1,)
```

- [ ] **Step 2: Run the focused tests and verify RED**

```bash
python3 -m pytest tests/test_storage.py -v
```

Expected: import failure because `entropy_arb.storage` does not exist.

- [ ] **Step 3: Implement `entropy_arb/storage.py` with the exact v1 contracts**

Use immutable dataclasses for rows and explicit table specs. Define these constants and types exactly:

```python
SCHEMA_VERSION = "1"
FLUSH_INTERVAL_SEC = 10.0
MAX_PENDING_ROWS_PER_DATASET = 100_000
DEFAULT_BUSY_TIMEOUT_MS = 10_000

@dataclass(frozen=True)
class SampleRow:
    timestamp_ms: int
    symbol: str
    hedge: str
    premium_bps: float
    sell_edge_bps: float
    buy_edge_bps: float
    entropy_bid: float
    entropy_ask: float
    hedge_bid: float
    hedge_ask: float
    entropy_book_update_ms: int
    hedge_book_update_ms: int

@dataclass(frozen=True)
class MinuteRow:
    minute_ts: int
    symbol: str
    hedge: str
    entropy_bid: float
    entropy_ask: float
    hedge_bid: float
    hedge_ask: float
    premium_open_bps: float
    premium_high_bps: float
    premium_low_bps: float
    premium_close_bps: float
    premium_mean_bps: float
    premium_std_bps: float
    sell_edge_mean_bps: float
    sell_edge_max_bps: float
    buy_edge_mean_bps: float
    buy_edge_max_bps: float
    samples: int

@dataclass(frozen=True)
class EntropyReferenceRow:
    symbol: str
    hedge: str
    recv_ms: int
    oracle_px: float
    mark_px: float

@dataclass(frozen=True)
class HedgeReferenceRow:
    symbol: str
    hedge: str
    recv_ms: int
    server_ms: int
    index_px: float
    mark_px: float

@dataclass(frozen=True)
class InsertCounts:
    inserted: int = 0
    duplicates: int = 0
    conflicts: int = 0

@dataclass(frozen=True)
class FlushReport:
    ok: bool
    datasets: dict[str, InsertCounts]
```

Create the five tables exactly as approved in the spec, including these primary keys:

```text
samples:           PRIMARY KEY (symbol, hedge, timestamp_ms)
minutes:           PRIMARY KEY (symbol, hedge, minute_ts)
entropy_reference: PRIMARY KEY (symbol, hedge, recv_ms, oracle_px, mark_px)
hedge_reference:   PRIMARY KEY (symbol, hedge, recv_ms, server_ms, index_px, mark_px)
meta:              PRIMARY KEY (key)
```

`MarketHistoryStore.__init__` must create the parent directory, connect with `check_same_thread=False`, set the four PRAGMAs, run schema creation in one transaction, set `schema_version`/`created_at_utc` on a fresh DB, and raise a clear `RuntimeError` if an existing `schema_version` is not `1`.

Use one `threading.Lock` for connection operations and one for buffers. `append_*()` must only append to the matching Python list under the buffer lock. If that dataset already has `MAX_PENDING_ROWS_PER_DATASET` pending rows, increment a per-dataset dropped counter and log `CRITICAL` on the first drop and every 1,000th drop; never silently evict an older row.

`flush()` must snapshot buffers without clearing, write all snapshotted datasets inside one `BEGIN`/`COMMIT`, leave buffers untouched on any `sqlite3.Error`, and remove exactly the snapshotted prefixes only after commit succeeds.

For each row use `INSERT ... ON CONFLICT DO NOTHING`. If `cursor.rowcount == 0`, query the existing row by primary key and compare the complete payload. Equal payload increments `duplicates`; different payload increments `conflicts`, logs `ERROR`, and never uses `REPLACE`/`UPDATE`.

Expose:

```python
@property
def pending_rows(self) -> dict[str, int]:
    return {name: len(rows) for name, rows in self._buffers.items()}

@property
def dropped_rows(self) -> dict[str, int]:
    return dict(self._dropped_rows)

def import_rows(self, dataset: str, rows: Sequence[object]) -> InsertCounts:
    # one transaction; DB errors propagate for offline migration

def set_meta(self, key: str, value: str) -> None:
    # INSERT ... ON CONFLICT(key) DO UPDATE SET value=excluded.value

def close(self) -> None:
    # final flush; log failed flush; close connection under _db_lock
```

- [ ] **Step 4: Run storage tests GREEN**

```bash
python3 -m pytest tests/test_storage.py -v
```

Expected: all Task 1 tests pass.

- [ ] **Step 5: Run schema inspection and diff checks**

```bash
python3 - <<'PY'
import sqlite3, tempfile
from pathlib import Path
from entropy_arb.storage import MarketHistoryStore
p = Path(tempfile.mkdtemp()) / "history.sqlite"
MarketHistoryStore(p).close()
with sqlite3.connect(p) as c:
    print(c.execute("PRAGMA journal_mode").fetchone())
    print(c.execute("PRAGMA quick_check").fetchone())
    print(c.execute("SELECT key,value FROM meta ORDER BY key").fetchall())
PY
git diff --check
```

Expected: `wal`, `ok`, schema version `1`, and no whitespace errors.

- [ ] **Step 6: Commit Task 1**

```bash
git add entropy_arb/storage.py tests/test_storage.py
git commit -m "feat: add sqlite market history store"
git push
```

---

### Task 2: Prove real multi-process WAL behavior and failure buffering

**Files:**
- Modify: `tests/test_storage.py`
- Modify only if a failing test proves it necessary: `entropy_arb/storage.py`

**Interfaces:**
- Consumes: `MarketHistoryStore`, `SampleRow`, production WAL policy from Task 1.
- Produces no new public API unless the tests expose a correctness bug.

- [ ] **Step 1: Add a real multi-process writer test**

Append this top-level worker and test to `tests/test_storage.py`:

```python
import multiprocessing as mp


def _write_process(db_path: str, symbol: str, start_ms: int, count: int) -> None:
    store = MarketHistoryStore(db_path)
    for i in range(count):
        row = sample(ts=start_ms + i)
        row = SampleRow(**{**row.__dict__, "symbol": symbol})
        store.append_sample(row)
    report = store.flush()
    if not report.ok:
        raise RuntimeError("child flush failed")
    store.close()


def test_two_real_processes_write_same_wal_database(tmp_path: Path):
    db = tmp_path / "market-history.sqlite"
    MarketHistoryStore(db).close()
    ctx = mp.get_context("spawn")
    p1 = ctx.Process(target=_write_process, args=(str(db), "SNDK", 1_700_000_000_000, 200))
    p2 = ctx.Process(target=_write_process, args=(str(db), "ANTH", 1_700_001_000_000, 200))
    p1.start()
    p2.start()
    p1.join(20)
    p2.join(20)
    assert p1.exitcode == 0
    assert p2.exitcode == 0

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone() == (400,)
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
```

- [ ] **Step 2: Add transient-lock retention and hard-cap tests**

```python
def test_busy_flush_keeps_pending_rows_for_retry(tmp_path: Path):
    db = tmp_path / "market-history.sqlite"
    store = MarketHistoryStore(db, busy_timeout_ms=50)
    blocker = sqlite3.connect(db, timeout=0.05)
    blocker.execute("PRAGMA journal_mode=WAL")
    blocker.execute("BEGIN IMMEDIATE")
    store.append_sample(sample())

    report = store.flush()
    assert not report.ok
    assert store.pending_rows["samples"] == 1

    blocker.rollback()
    blocker.close()
    retry = store.flush()
    assert retry.ok
    assert retry.datasets["samples"].inserted == 1
    store.close()


def test_pending_buffer_cap_counts_drops_without_evicting_old_rows(tmp_path: Path):
    db = tmp_path / "market-history.sqlite"
    store = MarketHistoryStore(db, max_pending_rows_per_dataset=2)
    store.append_sample(sample(ts=1))
    store.append_sample(sample(ts=2))
    store.append_sample(sample(ts=3))
    assert store.pending_rows["samples"] == 2
    assert store.dropped_rows["samples"] == 1
    assert store.flush().ok
    store.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT timestamp_ms FROM samples ORDER BY timestamp_ms"
        ).fetchall() == [(1,), (2,)]
```

- [ ] **Step 3: Run RED/GREEN loop**

```bash
python3 -m pytest tests/test_storage.py -v
```

If a test fails, make the smallest storage-only fix. Do not add a cross-process lock manager or worker daemon.

- [ ] **Step 4: Commit Task 2**

```bash
git add entropy_arb/storage.py tests/test_storage.py
git diff --check
git commit -m "test: verify concurrent market history writes"
git push
```

---

### Task 3: Atomic live persistence cutover — recorder, reference, config, and Engine lifecycle

This is one review gate because the four pieces must switch together: a shared store path is useless until Engine owns the store, and reference data cannot share the connection until its CSV writer is replaced. The steps remain individually RED/GREEN inside the task.

**Files:**
- Modify: `entropy_arb/config.py`
- Modify: `config.example.yaml`
- Modify: `entropy_arb/recorder.py`
- Modify: `entropy_arb/reference.py`
- Modify: `entropy_arb/engine.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_recorder.py`
- Modify: `tests/test_reference.py`
- Modify: `tests/test_engine.py`
- Modify: `.gitignore`

**Interfaces:**
- Config produces: `Config.recorder_database: str`, default `data/market-history.sqlite`.
- `MinuteRecorder(store, entropy_book, hedge_book, staleness_sec, interval_sec=1.0, *, symbol, hedge)` appends rows but never owns/closes the shared store.
- `ReferenceRecorder(..., store: MarketHistoryStore)` uses store-backed writers; parsers/feed behavior remains unchanged.
- `Engine.market_history: Optional[MarketHistoryStore]` owns exactly one store per process.
- `Engine._storage_flush_loop()` calls `await asyncio.to_thread(store.flush)` every `FLUSH_INTERVAL_SEC`.

- [ ] **Step 1: Change config tests to the final database contract**

In `tests/test_config.py`, replace recorder CSV assertions with:

```python
def test_recorder_database_defaults_and_is_shared():
    cfg_a = load(MINIMAL, symbol="SNDK", hedge="lighter-rh")
    cfg_b = load(MINIMAL, symbol="ANTH", hedge="lighter")
    assert cfg_a.recorder_enabled is True
    assert cfg_a.recorder_database == "data/market-history.sqlite"
    assert cfg_b.recorder_database == "data/market-history.sqlite"


def test_custom_recorder_database_is_preserved():
    cfg = load(
        MINIMAL + "\nrecorder:\n  database: archive/history.sqlite\n",
        symbol="SNDK",
        hedge="lighter-rh",
    )
    assert cfg.recorder_database == "archive/history.sqlite"


def test_legacy_recorder_csv_gets_actionable_error():
    expect_error(
        MINIMAL + "\nrecorder:\n  csv: logs/minutes.csv\n",
        "legacy 'recorder.csv' is no longer supported",
    )
```

Run `python3 -m pytest tests/test_config.py -v` and verify RED.

- [ ] **Step 2: Implement the config migration**

In `entropy_arb/config.py` define `DEFAULT_RECORDER_DATABASE = "data/market-history.sqlite"`; replace `Config.recorder_csv` with `Config.recorder_database`; replace schema key `csv` with `database`; remove `_resolve_recorder_csv`.

Before generic `_validate`, detect `recorder.csv` and raise exactly:

```text
legacy 'recorder.csv' is no longer supported; use
recorder.database: data/market-history.sqlite
```

Load with:

```python
recorder_database=str(_get(raw, "recorder", "database", DEFAULT_RECORDER_DATABASE))
```

Update `config.example.yaml`:

```yaml
recorder:
  enabled: true
  database: data/market-history.sqlite
```

Update its comments to describe SQLite storage and remove `tools/analyze.py` as the normal strategy-selection workflow. Re-run config tests GREEN.

- [ ] **Step 3: Rewrite recorder parity tests against SQLite, then implement**

In `tests/test_recorder.py`, remove tests that only verify CSV headers/path/flush mechanics. Keep stale-book and aggregation semantics. Use a real `MarketHistoryStore`, call `rec.sample()`, `rec.close()`, `store.flush()`, then query SQLite.

Main assertions must include exact sample timestamp, `calculate_premiums()` equivalent premium/sell/buy values, book update timestamps, minute sample count, open/high/close/mean, and final BBO close values.

Then refactor `entropy_arb/recorder.py`:

- remove `csv`, path derivation, headers, file handles, and CSV flush code;
- preserve `_MinuteAgg.add()` math unchanged;
- make `_MinuteAgg.row()` return `MinuteRow` with existing persisted precision (`.10g` prices, `.3f` bps/statistics converted back to float);
- `sample()` appends a full-precision `SampleRow`;
- `_flush_agg()` appends `MinuteRow`;
- `close()` flushes only the partial minute and never closes the shared store.

Final constructor:

```python
def __init__(
    self,
    store: MarketHistoryStore,
    entropy_book: OrderBook,
    hedge_book: OrderBook,
    staleness_sec: float,
    interval_sec: float = 1.0,
    *,
    symbol: str,
    hedge: str,
) -> None:
```

Run `python3 -m pytest tests/test_recorder.py -v` until GREEN.

- [ ] **Step 4: Replace reference CSV persistence with store-backed adapters**

Keep reference header constants, parsers, feed subscriptions, reconnect behavior, filtering, and timestamps unchanged. Replace `ReferenceCsvWriter` with `EntropyReferenceStoreWriter` and `HedgeReferenceStoreWriter`; both expose `enabled`, `write`, `stop_accepting`, `close` but `write()` only builds the canonical row and calls the matching `store.append_*()`.

Entropy adapter payload:

```python
recv_ms, oracle_px, mark_px = row
EntropyReferenceRow(symbol, hedge, int(recv_ms), float(oracle_px), float(mark_px))
```

Hedge adapter payload:

```python
recv_ms, server_ms, index_px, mark_px = row
HedgeReferenceRow(symbol, hedge, int(recv_ms), int(server_ms), float(index_px), float(mark_px))
```

Change `ReferenceRecorder.__init__` to require `store: MarketHistoryStore`; remove live CSV path creation. Update reference tests to query both SQLite reference tables while leaving parser/feed fake-writer tests intact. Run `python3 -m pytest tests/test_reference.py -v` until GREEN.

- [ ] **Step 5: Wire exactly one shared store into Engine**

In `Engine.__init__`:

```python
self.market_history: Optional[MarketHistoryStore] = None
```

In `_run_inner()`, after venue market loading and before recorder/reference construction:

```python
if cfg.recorder_enabled or self.record_only:
    self.market_history = MarketHistoryStore(cfg.recorder_database)
```

Pass the same object to `MinuteRecorder` and `ReferenceRecorder`.

Add a background flush coroutine:

```python
async def _storage_flush_loop(self) -> None:
    while not self.stop.is_set():
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=FLUSH_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass
        if self.stop.is_set():
            break
        if self.market_history is not None:
            await asyncio.to_thread(self.market_history.flush)
```

Start it whenever `market_history` exists. Never run potentially blocking SQLite writes directly in strategy/feed/recorder callbacks.

Shutdown order:

```text
stop -> settle executions -> cancel/await normal tasks -> await reference shutdown
-> close venues -> asyncio.to_thread(market_history.close)
```

Store initialization/schema failure is startup-fatal. After successful startup, transient write failure only retains/retries buffers and logs; it must not change strategy/risk logic.

Update `tests/test_engine.py` fixture YAML from `recorder.csv` to a temp `recorder.database`. Add one lifecycle test proving recorder/reference share `Engine.market_history`; do not alter signal math tests.

- [ ] **Step 6: Protect runtime databases from git**

Add to `.gitignore`:

```gitignore
data/
exports/
```

Keep `logs/` because trade CSV and engine logs remain there.

- [ ] **Step 7: Run focused + full regression**

```bash
python3 -m pytest tests/test_config.py tests/test_recorder.py tests/test_reference.py tests/test_engine.py -v
python3 -m pytest tests/ -q
git diff --check
```

Expected: all pass; no live market-history CSV writer remains.

- [ ] **Step 8: Commit Task 3**

```bash
git add entropy_arb/config.py config.example.yaml entropy_arb/recorder.py \
        entropy_arb/reference.py entropy_arb/engine.py .gitignore \
        tests/test_config.py tests/test_recorder.py tests/test_reference.py tests/test_engine.py
git commit -m "feat: persist live market history in sqlite"
git push
```

---

### Task 4: Non-destructive legacy CSV migration tool

**Files:**
- Create: `entropy_arb/migration.py`
- Create: `tools/migrate_market_history.py`
- Create: `tests/test_migration.py`

**Interfaces:**
- Produces `MigrationFileReport`.
- Produces `migrate_directory(source: Path, database: Path, mappings: dict[str, tuple[str, str]]) -> list[MigrationFileReport]`.
- CLI: `python3 tools/migrate_market_history.py --source logs --database data/market-history.sqlite [--map FILE=SYMBOL,HEDGE ...]`.
- Consumes `MarketHistoryStore.import_rows()` and canonical row types.

- [ ] **Step 1: Write migration tests first**

Create `tests/test_migration.py` with concrete fixtures and assertions. Start with these helpers and canonical headers:

```python
import csv
import sqlite3
from pathlib import Path

from entropy_arb.migration import migrate_directory

SAMPLE_HEADER = [
    "timestamp_ms", "premium_bps", "sell_edge_bps", "buy_edge_bps",
    "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
    "entropy_book_update_ms", "hedge_book_update_ms",
]
MINUTE_HEADER = [
    "minute_ts", "time_utc", "symbol", "hedge",
    "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
    "premium_open_bps", "premium_high_bps", "premium_low_bps",
    "premium_close_bps", "premium_mean_bps", "premium_std_bps",
    "sell_edge_mean_bps", "sell_edge_max_bps",
    "buy_edge_mean_bps", "buy_edge_max_bps", "samples",
]
ENTROPY_REFERENCE_HEADER = ["recv_ms", "oracle_px", "mark_px"]
HEDGE_REFERENCE_HEADER = ["recv_ms", "server_ms", "index_px", "mark_px"]


def write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def report_for(reports, filename: str):
    return next(r for r in reports if Path(r.path).name == filename)


def sample_row(ts=1_700_000_000_000, premium=10.0):
    return [ts, premium, 8.0, -12.0, 100.09, 100.11, 99.99, 100.01, ts - 500, ts - 300]


def minute_row(ts=1_699_999_980):
    return [
        ts, "2023-11-14T22:13:00Z", "SNDK", "lighter-rh",
        100.09, 100.11, 99.99, 100.01,
        10.0, 20.0, 10.0, 20.0, 15.0, 5.0,
        13.0, 18.0, -17.0, -12.0, 2,
    ]
```

Then implement these exact tests:

```python
def test_valid_samples_minutes_and_reference_import(tmp_path: Path):
    src = tmp_path / "logs"
    src.mkdir()
    db = tmp_path / "history.sqlite"
    write_csv(src / "samples-v2-SNDK-lighter-rh.csv", SAMPLE_HEADER, [sample_row()])
    write_csv(src / "minutes-SNDK-lighter-rh.csv", MINUTE_HEADER, [minute_row()])
    write_csv(
        src / "reference-SNDK-lighter-rh-entropy.csv",
        ENTROPY_REFERENCE_HEADER,
        [[1_700_000_000_100, 100.0, 100.1]],
    )
    write_csv(
        src / "reference-SNDK-lighter-rh.csv",
        HEDGE_REFERENCE_HEADER,
        [[1_700_000_000_200, 1_700_000_000_150, 99.9, 100.0]],
    )

    reports = migrate_directory(src, db, {})
    assert {r.status for r in reports} == {"PASS"}
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM minutes").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM entropy_reference").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM hedge_reference").fetchone() == (1,)


def test_rerun_is_idempotent(tmp_path: Path):
    src = tmp_path / "logs"
    src.mkdir()
    db = tmp_path / "history.sqlite"
    name = "samples-v2-SNDK-lighter-rh.csv"
    write_csv(src / name, SAMPLE_HEADER, [sample_row()])
    first = report_for(migrate_directory(src, db, {}), name)
    second = report_for(migrate_directory(src, db, {}), name)
    assert first.inserted_rows == 1
    assert second.inserted_rows == 0
    assert second.already_existing == 1
    assert second.status == "PASS"


def test_duplicate_rows_are_counted(tmp_path: Path):
    src = tmp_path / "logs"
    src.mkdir()
    db = tmp_path / "history.sqlite"
    name = "samples-v2-SNDK-lighter-rh.csv"
    row = sample_row()
    write_csv(src / name, SAMPLE_HEADER, [row, row])
    report = report_for(migrate_directory(src, db, {}), name)
    assert report.source_rows == 2
    assert report.valid_rows == 2
    assert report.inserted_rows == 1
    assert report.already_existing == 1
    assert report.conflicting_key_rows == 0
    assert report.status == "PASS"


def test_same_key_different_payload_is_conflict_and_does_not_replace(tmp_path: Path):
    src = tmp_path / "logs"
    src.mkdir()
    db = tmp_path / "history.sqlite"
    name = "samples-v2-SNDK-lighter-rh.csv"
    write_csv(src / name, SAMPLE_HEADER, [sample_row(premium=10.0), sample_row(premium=999.0)])
    report = report_for(migrate_directory(src, db, {}), name)
    assert report.inserted_rows == 1
    assert report.conflicting_key_rows == 1
    assert report.status == "PARTIAL"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT premium_bps FROM samples").fetchone() == (10.0,)


def test_invalid_numeric_row_is_counted_and_valid_rows_still_import(tmp_path: Path):
    src = tmp_path / "logs"
    src.mkdir()
    db = tmp_path / "history.sqlite"
    name = "samples-v2-SNDK-lighter-rh.csv"
    bad = sample_row(ts=1_700_000_000_001)
    bad[4] = "not-a-price"
    write_csv(src / name, SAMPLE_HEADER, [sample_row(), bad])
    report = report_for(migrate_directory(src, db, {}), name)
    assert report.source_rows == 2
    assert report.valid_rows == 1
    assert report.invalid_rows == 1
    assert report.inserted_rows == 1
    assert report.status == "PARTIAL"


def test_unknown_schema_fails_closed(tmp_path: Path):
    src = tmp_path / "logs"
    src.mkdir()
    db = tmp_path / "history.sqlite"
    name = "samples-v2-SNDK-lighter-rh.csv"
    write_csv(src / name, ["timestamp", "price"], [[1, 100]])
    report = report_for(migrate_directory(src, db, {}), name)
    assert report.status == "FAIL"
    assert report.inserted_rows == 0


def test_ambiguous_samples_without_companion_needs_mapping(tmp_path: Path):
    src = tmp_path / "logs"
    src.mkdir()
    db = tmp_path / "history.sqlite"
    name = "samples-v2.csv"
    write_csv(src / name, SAMPLE_HEADER, [sample_row()])
    report = report_for(migrate_directory(src, db, {}), name)
    assert report.status == "NEEDS_MAPPING"
    assert report.inserted_rows == 0


def test_explicit_samples_mapping_succeeds(tmp_path: Path):
    src = tmp_path / "logs"
    src.mkdir()
    db = tmp_path / "history.sqlite"
    name = "samples-v2.csv"
    write_csv(src / name, SAMPLE_HEADER, [sample_row()])
    reports = migrate_directory(src, db, {name: ("SNDK", "lighter-rh")})
    report = report_for(reports, name)
    assert report.status == "PASS"
    assert report.inserted_rows == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT symbol,hedge FROM samples").fetchone() == ("SNDK", "lighter-rh")


def test_original_csv_bytes_and_mtime_are_unchanged(tmp_path: Path):
    src = tmp_path / "logs"
    src.mkdir()
    db = tmp_path / "history.sqlite"
    path = src / "samples-v2-SNDK-lighter-rh.csv"
    write_csv(path, SAMPLE_HEADER, [sample_row()])
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    migrate_directory(src, db, {})
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime
```

Canonical headers:

```python
SAMPLE_HEADER = [
    "timestamp_ms", "premium_bps", "sell_edge_bps", "buy_edge_bps",
    "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
    "entropy_book_update_ms", "hedge_book_update_ms",
]
MINUTE_HEADER = [
    "minute_ts", "time_utc", "symbol", "hedge",
    "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
    "premium_open_bps", "premium_high_bps", "premium_low_bps",
    "premium_close_bps", "premium_mean_bps", "premium_std_bps",
    "sell_edge_mean_bps", "sell_edge_max_bps",
    "buy_edge_mean_bps", "buy_edge_max_bps", "samples",
]
ENTROPY_REFERENCE_HEADER = ["recv_ms", "oracle_px", "mark_px"]
HEDGE_REFERENCE_HEADER = ["recv_ms", "server_ms", "index_px", "mark_px"]
```

Ambiguous samples fixture: `samples-v2.csv` with no `minutes.csv`. Mapping syntax:

```text
--map samples-v2.csv=SNDK,lighter-rh
```

Run `python3 -m pytest tests/test_migration.py -v` and verify RED.

- [ ] **Step 2: Implement discovery/provenance**

In `entropy_arb/migration.py`:

```python
MIGRATION_BATCH_ROWS = 5_000
SUPPORTED_HEDGES = ("lighter-rh", "tradexyz", "lighter")
```

Only inspect `samples-v2*.csv`, `minutes*.csv`, `reference-*.csv`; ignore trades/arbitrary CSVs. Header must exactly match a supported schema or that candidate reports `FAIL`.

Samples provenance resolution order:

```text
1. parse filename from the right using known hedge suffixes (lighter-rh before lighter)
2. if unresolved, companion minutes file (samples-v2 prefix -> minutes prefix) and require one unambiguous pair
3. if unresolved, explicit basename mapping
4. otherwise NEEDS_MAPPING and insert nothing from that sample file
```

Reference filenames resolve pair from `reference-{symbol}-{hedge}-entropy.csv` or `reference-{symbol}-{hedge}.csv`. Minute row symbol/hedge is authoritative.

- [ ] **Step 3: Implement row validation and batched import**

Validation:

```text
timestamps -> integer > 0
prices -> finite float > 0
premium/edge/stat values -> finite float, any sign
minute samples -> integer > 0
symbol -> non-empty
hedge -> lighter | lighter-rh | tradexyz
```

Do not recompute values, fill gaps, sort/rewrite/move source files. Import valid rows in batches of 5,000 via `store.import_rows()`.

Define:

```python
@dataclass(frozen=True)
class MigrationFileReport:
    path: str
    dataset: str | None
    source_rows: int
    valid_rows: int
    invalid_rows: int
    already_existing: int
    conflicting_key_rows: int
    inserted_rows: int
    min_timestamp: int | None
    max_timestamp: int | None
    status: str
    message: str
```

Status:

```text
PASS -> supported, provenance resolved, no invalid/conflict rows
PARTIAL -> valid rows imported but invalid and/or conflict rows exist
NEEDS_MAPPING -> samples provenance unresolved, no insert from that file
FAIL -> unsupported schema, unsafe parse/open failure, or database failure
```

Exact duplicates alone remain PASS. After at least one PASS/PARTIAL file, set `last_migration_at_utc` in meta to current UTC ISO second.

- [ ] **Step 4: Implement CLI and exit codes**

`tools/migrate_market_history.py` options:

```text
--source default logs
--database default data/market-history.sqlite
--map repeatable FILE=SYMBOL,HEDGE
```

Print per-file counters/status and an overall summary. Exit `0` if no FAIL/NEEDS_MAPPING, `2` if NEEDS_MAPPING exists and no FAIL, `1` if any FAIL.

- [ ] **Step 5: Run tests + rerun smoke**

```bash
python3 -m pytest tests/test_migration.py -v
python3 -m pytest tests/test_storage.py tests/test_migration.py -q
git diff --check
```

Create a tiny temp source, run the CLI twice, and verify second run inserts zero new exact duplicates. Do not point this task at production `logs/`.

- [ ] **Step 6: Commit Task 4**

```bash
git add entropy_arb/migration.py tools/migrate_market_history.py tests/test_migration.py
git commit -m "feat: migrate legacy market history csv"
git push
```

---

### Task 5: Consistent standalone snapshot tool

**Files:**
- Create: `entropy_arb/snapshot.py`
- Create: `tools/snapshot_data.py`
- Create: `tests/test_snapshot.py`

**Interfaces:**
- Produces `create_snapshot(source: Path, destination: Path) -> Path`.
- Defaults: source `data/market-history.sqlite`, output `exports/market-history-snapshot-YYYYMMDD-HHMMSSZ.sqlite`.
- Must use `sqlite3.Connection.backup()` then `PRAGMA quick_check`.

- [ ] **Step 1: Write snapshot tests first**

Create `tests/test_snapshot.py` with a real source database and a spawned concurrent writer:

```python
import multiprocessing as mp
import sqlite3
import time
from pathlib import Path

import pytest

from entropy_arb.snapshot import create_snapshot
from entropy_arb.storage import MarketHistoryStore, SampleRow


def sample(ts: int) -> SampleRow:
    return SampleRow(
        timestamp_ms=ts, symbol="SNDK", hedge="lighter-rh",
        premium_bps=0.0, sell_edge_bps=0.0, buy_edge_bps=0.0,
        entropy_bid=100.0, entropy_ask=100.1, hedge_bid=100.0, hedge_ask=100.1,
        entropy_book_update_ms=ts, hedge_book_update_ms=ts,
    )


def _snapshot_writer(db_path: str, ready, stop) -> None:
    store = MarketHistoryStore(db_path)
    i = 0
    try:
        while not stop.is_set():
            store.append_sample(sample(1_700_000_000_000 + i))
            report = store.flush()
            if not report.ok:
                raise RuntimeError("writer flush failed")
            ready.set()
            i += 1
            time.sleep(0.01)
    finally:
        store.close()


def test_snapshot_is_standalone_and_quick_check_ok(tmp_path: Path):
    source = tmp_path / "live.sqlite"
    destination = tmp_path / "snapshot.sqlite"
    store = MarketHistoryStore(source)
    store.append_sample(sample(1_700_000_000_000))
    assert store.flush().ok
    create_snapshot(source, destination)
    store.close()

    assert destination.exists()
    with sqlite3.connect(destination) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone() == (1,)


def test_snapshot_is_consistent_while_writer_is_active(tmp_path: Path):
    source = tmp_path / "live.sqlite"
    destination = tmp_path / "snapshot.sqlite"
    MarketHistoryStore(source).close()
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    stop = ctx.Event()
    writer = ctx.Process(target=_snapshot_writer, args=(str(source), ready, stop))
    writer.start()
    assert ready.wait(10)
    create_snapshot(source, destination)
    stop.set()
    writer.join(10)
    assert writer.exitcode == 0

    with sqlite3.connect(destination) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] >= 1


def test_destination_already_exists_is_rejected_without_overwrite(tmp_path: Path):
    source = tmp_path / "live.sqlite"
    destination = tmp_path / "snapshot.sqlite"
    MarketHistoryStore(source).close()
    destination.write_bytes(b"sentinel")
    with pytest.raises(FileExistsError):
        create_snapshot(source, destination)
    assert destination.read_bytes() == b"sentinel"
```

Run `python3 -m pytest tests/test_snapshot.py -v` and verify RED.

- [ ] **Step 2: Implement snapshot library**

`entropy_arb/snapshot.py`:

```python
from pathlib import Path
import sqlite3


def create_snapshot(source: Path, destination: Path) -> Path:
    source = Path(source)
    destination = Path(destination)
    if not source.exists():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)

    src = sqlite3.connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"snapshot quick_check failed: {result!r}")
        dst.commit()
    except BaseException:
        dst.close()
        src.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        dst.close()
        src.close()
        return destination
```

No filesystem copy, source checkpoint, `VACUUM`, source pause, or `-wal`/`-shm` copy.

- [ ] **Step 3: Implement CLI**

`tools/snapshot_data.py`:

```text
--database default data/market-history.sqlite
--output optional
```

Without `--output`, use UTC `datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")` and write under `exports/`. Success line includes source, output, `quick_check=ok`; failure exits nonzero and leaves no partial file.

- [ ] **Step 4: Run tests + manual smoke**

```bash
python3 -m pytest tests/test_snapshot.py -v
python3 -m pytest tests/test_storage.py tests/test_snapshot.py -q
git diff --check
```

Also create one temp live DB, snapshot it, close the source, and open/query only the snapshot. Verify no sidecar file is required.

- [ ] **Step 5: Commit Task 5**

```bash
git add entropy_arb/snapshot.py tools/snapshot_data.py tests/test_snapshot.py
git commit -m "feat: add consistent market history snapshots"
git push
```

---

### Task 6: Documentation, full regression, migration rehearsal, and live record-only gate

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify only documentation comments if needed: `tools/analyze.py`
- No Strategy/Planner/Venue execution change is allowed.

**Interfaces:**
- Documents live SQLite -> optional legacy migration -> snapshot -> one-file upload.
- Labels `tools/analyze.py` as legacy CSV ad-hoc only.

- [ ] **Step 1: Update both READMEs**

Communicate exactly:

```text
recorder.enabled stores ~1 Hz raw BBO samples + minute aggregates in recorder.database
supported Lighter reference data is stored in the same database
default path is data/market-history.sqlite
multiple bot processes share it through SQLite WAL
--record-only writes the same DB without trading credentials
old CSVs import non-destructively via tools/migrate_market_history.py
tools/snapshot_data.py creates one standalone SQLite snapshot while bots are active
upload that snapshot for manual/ChatGPT analysis
bot does not diagnose/select/switch strategies
tools/analyze.py is legacy CSV ad-hoc only
```

Configuration table entry:

```text
recorder.enabled / recorder.database | market-history storage | true / data/market-history.sqlite
```

Layout must list `storage.py`, `migration.py`, `snapshot.py`, and the two new tools. Mirror the semantics in `README.zh-CN.md`.

- [ ] **Step 2: Search stale instructions**

```bash
grep -RInE 'recorder_csv|recorder\.csv|logs/minutes\.csv|1-minute CSV|minute CSV|tools/analyze\.py' \
  README.md README.zh-CN.md config.example.yaml entropy_arb tests tools \
  --exclude='test_analyze.py'
```

Allowed remaining matches: intentional `recorder.csv` migration error/test; legacy migration fixtures; explicit text labeling `tools/analyze.py` legacy. Live recorder/reference must have no CSV persistence.

- [ ] **Step 3: Full automated verification**

```bash
rm -rf .pytest_cache
python3 -m pytest tests/ -q
python3 -m pytest -p no:cacheprovider tests/ -q
git diff --check
```

Record exact pass counts for both runs.

- [ ] **Step 4: Rehearse migration on copies only**

Copy at least one real existing `samples-v2`, matching `minutes`, Entropy reference, and hedge reference CSV into a temporary directory when available. Run migration twice into a temporary DB. Verify second run inserts zero additional exact duplicates and `PRAGMA quick_check` returns `ok`.

If a real reference CSV is unavailable, use test fixtures for that table and say so. Do not run production migration against original `logs/` yet.

- [ ] **Step 5: Live record-only smoke with temporary database**

Use a temporary config containing:

```yaml
recorder:
  enabled: true
  database: /tmp/entropy-arb-storage-v1-smoke.sqlite
```

Run the previously known-good public-feed pair for 30–60 seconds, stop with SIGINT, then query:

```bash
python3 - <<'PY'
import sqlite3
p = "/tmp/entropy-arb-storage-v1-smoke.sqlite"
with sqlite3.connect(p) as c:
    print("quick_check", c.execute("PRAGMA quick_check").fetchone())
    for table in ("samples", "minutes", "entropy_reference", "hedge_reference"):
        print(table, c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
PY
```

Acceptance: `quick_check=ok`, samples > 0, graceful shutdown writes partial minute if any valid sample existed, Lighter reference tables > 0 when external feeds are available, zero orders/trades, clean SIGINT exit.

- [ ] **Step 6: Snapshot the smoke database**

```bash
python3 tools/snapshot_data.py \
  --database /tmp/entropy-arb-storage-v1-smoke.sqlite \
  --output /tmp/entropy-arb-storage-v1-smoke-snapshot.sqlite
python3 - <<'PY'
import sqlite3
p = "/tmp/entropy-arb-storage-v1-smoke-snapshot.sqlite"
with sqlite3.connect(p) as c:
    print(c.execute("PRAGMA quick_check").fetchone())
    print(c.execute("SELECT COUNT(*) FROM samples").fetchone())
PY
```

Expected: standalone `ok` snapshot without source sidecars.

- [ ] **Step 7: Final scope audit**

```bash
DOCS_COMMIT=$(cat /tmp/entropy-arb-storage-v1-docs-commit)
git diff "$DOCS_COMMIT"...HEAD --stat
git diff "$DOCS_COMMIT"...HEAD -- entropy_arb/strategy.py entropy_arb/book.py entropy_arb/venue_hl.py entropy_arb/venue_lighter.py
git diff "$DOCS_COMMIT"...HEAD -- requirements.txt requirements-live.txt
```

Expected: no Strategy/Planner/Venue execution changes and no dependency changes. If present, stop and revert/justify before completion.

- [ ] **Step 8: Commit docs**

```bash
git add README.md README.zh-CN.md
git diff --cached --check
git commit -m "docs: document sqlite market history workflow"
git push
```

If only a legacy-label docstring in `tools/analyze.py` changed, include that file in this commit.

- [ ] **Step 9: Final execution report**

Report exact:

```text
starting docs commit SHA
final HEAD SHA
commits created
files changed
focused test counts
full-suite pass count normal + cache-free
multi-process WAL test result
migration rehearsal source-copy scope + rerun result
record-only smoke pair/duration + row counts
snapshot quick_check result
production legacy CSV migration NOT YET RUN
out-of-scope diff audit
```

Do not claim Storage v1 complete without fresh final-HEAD evidence.

---

## Plan self-review result

- **Spec coverage:** schema/meta, WAL policy, multi-process writing, raw/minute/reference persistence, nonblocking live integration, no-overwrite conflicts, buffer retry/cap, config migration, non-destructive legacy migration, provenance fail-closed rules, backup-API snapshot, docs, regression, and record-only verification all map to explicit tasks.
- **Scope:** no Market Analyzer, replay/backtest, export-by-date, Parquet, DuckDB live store, daemon, cloud, retention, or trade migration.
- **Type consistency:** all canonical row/store/result types are defined once in Task 1 and consumed under the same names later.
- **Operational clarification:** DB initialization/schema mismatch is startup-fatal; after successful initialization, transient write contention/errors keep buffered rows and retry off the asyncio event loop without modifying trading logic.
- **Sequencing:** recorder + reference + config + Engine ownership cut over atomically in Task 3 so no committed intermediate state silently ignores a path or stores only part of market history.
