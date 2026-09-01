"""SQLite persistence for market history observations."""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

SCHEMA_VERSION = "1"
FLUSH_INTERVAL_SEC = 10.0
MAX_PENDING_ROWS_PER_DATASET = 100_000
DEFAULT_BUSY_TIMEOUT_MS = 10_000

logger = logging.getLogger(__name__)


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


_SPECS = {
    "samples": (SampleRow, ("symbol", "hedge", "timestamp_ms"), (
        "timestamp_ms", "symbol", "hedge", "premium_bps", "sell_edge_bps", "buy_edge_bps",
        "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask", "entropy_book_update_ms", "hedge_book_update_ms")),
    "minutes": (MinuteRow, ("symbol", "hedge", "minute_ts"), (
        "minute_ts", "symbol", "hedge", "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
        "premium_open_bps", "premium_high_bps", "premium_low_bps", "premium_close_bps", "premium_mean_bps",
        "premium_std_bps", "sell_edge_mean_bps", "sell_edge_max_bps", "buy_edge_mean_bps", "buy_edge_max_bps", "samples")),
    "entropy_reference": (EntropyReferenceRow, ("symbol", "hedge", "recv_ms", "oracle_px", "mark_px"),
                           ("symbol", "hedge", "recv_ms", "oracle_px", "mark_px")),
    "hedge_reference": (HedgeReferenceRow, ("symbol", "hedge", "recv_ms", "server_ms", "index_px", "mark_px"),
                         ("symbol", "hedge", "recv_ms", "server_ms", "index_px", "mark_px")),
}

_CREATE = {
    "meta": "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "samples": """CREATE TABLE IF NOT EXISTS samples (
        timestamp_ms INTEGER NOT NULL, symbol TEXT NOT NULL, hedge TEXT NOT NULL,
        premium_bps REAL NOT NULL, sell_edge_bps REAL NOT NULL, buy_edge_bps REAL NOT NULL,
        entropy_bid REAL NOT NULL, entropy_ask REAL NOT NULL, hedge_bid REAL NOT NULL, hedge_ask REAL NOT NULL,
        entropy_book_update_ms INTEGER NOT NULL, hedge_book_update_ms INTEGER NOT NULL,
        PRIMARY KEY (symbol, hedge, timestamp_ms))""",
    "minutes": """CREATE TABLE IF NOT EXISTS minutes (
        minute_ts INTEGER NOT NULL, symbol TEXT NOT NULL, hedge TEXT NOT NULL,
        entropy_bid REAL NOT NULL, entropy_ask REAL NOT NULL, hedge_bid REAL NOT NULL, hedge_ask REAL NOT NULL,
        premium_open_bps REAL NOT NULL, premium_high_bps REAL NOT NULL, premium_low_bps REAL NOT NULL,
        premium_close_bps REAL NOT NULL, premium_mean_bps REAL NOT NULL, premium_std_bps REAL NOT NULL,
        sell_edge_mean_bps REAL NOT NULL, sell_edge_max_bps REAL NOT NULL, buy_edge_mean_bps REAL NOT NULL,
        buy_edge_max_bps REAL NOT NULL, samples INTEGER NOT NULL,
        PRIMARY KEY (symbol, hedge, minute_ts))""",
    "entropy_reference": "CREATE TABLE IF NOT EXISTS entropy_reference (symbol TEXT NOT NULL, hedge TEXT NOT NULL, recv_ms INTEGER NOT NULL, oracle_px REAL NOT NULL, mark_px REAL NOT NULL, PRIMARY KEY (symbol, hedge, recv_ms, oracle_px, mark_px))",
    "hedge_reference": "CREATE TABLE IF NOT EXISTS hedge_reference (symbol TEXT NOT NULL, hedge TEXT NOT NULL, recv_ms INTEGER NOT NULL, server_ms INTEGER NOT NULL, index_px REAL NOT NULL, mark_px REAL NOT NULL, PRIMARY KEY (symbol, hedge, recv_ms, server_ms, index_px, mark_px))",
}


class MarketHistoryStore:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
                 max_pending_rows_per_dataset: int = MAX_PENDING_ROWS_PER_DATASET):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._max_pending = max_pending_rows_per_dataset
        self._db_lock = threading.Lock()
        self._buffer_lock = threading.Lock()
        self._buffers = {name: [] for name in _SPECS}
        self._dropped_rows = {name: 0 for name in _SPECS}
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._db_lock:
            self._conn.execute("BEGIN")
            try:
                for sql in _CREATE.values():
                    self._conn.execute(sql)
                current = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if current and current[0] != SCHEMA_VERSION:
                    raise RuntimeError(f"unsupported market-history schema version: {current[0]}")
                if current is None:
                    now = datetime.now(timezone.utc).isoformat()
                    self._conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)", (SCHEMA_VERSION,))
                    self._conn.execute("INSERT INTO meta(key,value) VALUES('created_at_utc',?)", (now,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                self._conn.close()
                raise

    @property
    def pending_rows(self) -> dict[str, int]:
        with self._buffer_lock:
            return {name: len(rows) for name, rows in self._buffers.items()}

    @property
    def dropped_rows(self) -> dict[str, int]:
        with self._buffer_lock:
            return dict(self._dropped_rows)

    def _append(self, dataset: str, row: object) -> None:
        with self._buffer_lock:
            buf = self._buffers[dataset]
            if len(buf) >= self._max_pending:
                self._dropped_rows[dataset] += 1
                dropped = self._dropped_rows[dataset]
                if dropped == 1 or dropped % 1000 == 0:
                    logger.critical("dropped %d pending %s row(s) at buffer cap", dropped, dataset)
                return
            buf.append(row)

    def append_sample(self, row: SampleRow) -> None: self._append("samples", row)
    def append_minute(self, row: MinuteRow) -> None: self._append("minutes", row)
    def append_entropy_reference(self, row: EntropyReferenceRow) -> None: self._append("entropy_reference", row)
    def append_hedge_reference(self, row: HedgeReferenceRow) -> None: self._append("hedge_reference", row)

    def _write(self, dataset: str, rows: Sequence[object]) -> InsertCounts:
        _, keys, fields = _SPECS[dataset]
        quoted = ",".join(fields)
        placeholders = ",".join("?" for _ in fields)
        sql = f"INSERT INTO {dataset} ({quoted}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
        counts = [0, 0, 0]
        for row in rows:
            values = tuple(getattr(row, field) for field in fields)
            cur = self._conn.execute(sql, values)
            if cur.rowcount:
                counts[0] += 1
                continue
            where = " AND ".join(f"{key}=?" for key in keys)
            existing = self._conn.execute(f"SELECT {quoted} FROM {dataset} WHERE {where}", tuple(getattr(row, key) for key in keys)).fetchone()
            if existing == values:
                counts[1] += 1
            else:
                counts[2] += 1
                logger.error("conflicting %s row for key %s", dataset, tuple(getattr(row, key) for key in keys))
        return InsertCounts(*counts)

    def flush(self) -> FlushReport:
        with self._buffer_lock:
            snapshot = {name: tuple(rows) for name, rows in self._buffers.items() if rows}
        if not snapshot:
            return FlushReport(True, {})
        try:
            with self._db_lock:
                self._conn.execute("BEGIN")
                results = {name: self._write(name, rows) for name, rows in snapshot.items()}
                self._conn.commit()
            with self._buffer_lock:
                for name, rows in snapshot.items():
                    del self._buffers[name][:len(rows)]
            return FlushReport(True, results)
        except sqlite3.Error:
            with self._db_lock:
                self._conn.rollback()
            logger.exception("market-history flush failed")
            return FlushReport(False, {})

    def import_rows(self, dataset: str, rows: Sequence[object]) -> InsertCounts:
        if dataset not in _SPECS:
            raise ValueError(f"unknown dataset: {dataset}")
        with self._db_lock:
            self._conn.execute("BEGIN")
            try:
                result = self._write(dataset, rows)
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def set_meta(self, key: str, value: str) -> None:
        with self._db_lock:
            self._conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._conn.commit()

    def close(self) -> None:
        report = self.flush()
        if not report.ok:
            logger.error("market-history final flush failed")
        with self._db_lock:
            self._conn.close()
