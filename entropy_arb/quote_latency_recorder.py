"""Opt-in, public-feed-only high-frequency quote recorder.

This module is deliberately separate from ``Engine`` and ``main.py``.  It
opens only public websocket feeds, never loads ``.env``, and writes bounded
research observations rather than orders or account state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Optional

from .book import OrderBook
from .config import HL_WS_URL, LIGHTER_PROFILES
from .feeds import HLBookFeed, LighterBookFeed
from .quote_latency import (
    BoundedJsonlWriter,
    EdgeDecayTracker,
    FeedQuote,
    _parameter_fields,
    normalize_feed_update,
)
from .rolling_legacy_strategy import LegacyRollingStrategy

log = logging.getLogger("quote-latency-recorder")


class QuoteLatencyRecorder:
    """Capture exactly one explicit SNDK x Lighter RH public market pair."""

    def __init__(self, output_dir: str | Path, *, symbol: str,
                 hedge: str, hl_ws_url: str = HL_WS_URL,
                 rh_ws_url: Optional[str] = None, rh_market_id: int = 32,
                 hl_coin: Optional[str] = None, rolling_csv: str = "",
                 sell_quantile: float = 0.80, buy_quantile: float = 0.50,
                 fees_bps: float = 0.0, depth_levels: int = 5,
                 pair_max_skew_ms: float = 250.0, queue_size: int = 10_000,
                 max_file_rows: int = 50_000,
                 max_file_bytes: int = 64 * 1024 * 1024,
                 retention_hours: float = 24.0, max_files: int = 24,
                 preserve_raw: bool = False,
                 feed_factory: Optional[Callable] = None,
                 rolling_provider: Optional[object] = None) -> None:
        if symbol.upper() != "SNDK":
            raise ValueError("quote latency recorder is restricted to SNDK")
        if hedge != "lighter-rh":
            raise ValueError("quote latency recorder is restricted to lighter-rh")
        if int(rh_market_id) != 32:
            raise ValueError("SNDK lighter-rh market id must be 32")
        if depth_levels <= 0 or queue_size <= 0:
            raise ValueError("depth_levels and queue_size must be positive")
        self.output_dir = Path(output_dir)
        self.symbol = "SNDK"
        self.hedge = hedge
        self.market = "io:SNDK|lighter-rh:SNDK"
        self.hl_ws_url = hl_ws_url
        self.rh_ws_url = rh_ws_url or LIGHTER_PROFILES[hedge].ws_url
        self.rh_market_id = int(rh_market_id)
        self.hl_coin = hl_coin or "io:SNDK"
        self.depth_levels = depth_levels
        self.pair_max_skew_ms = pair_max_skew_ms
        self.queue_size = queue_size
        self.max_file_rows = max_file_rows
        self.max_file_bytes = max_file_bytes
        self.retention_hours = retention_hours
        self.max_files = max_files
        self.preserve_raw = bool(preserve_raw)
        self._feed_factory = feed_factory
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.books = {"entropy": OrderBook(), "lighter-rh": OrderBook()}
        self.tracker = EdgeDecayTracker(pair_max_skew_ms=pair_max_skew_ms)
        self.dropped_updates = 0
        self.dropped_lifecycle = 0
        self._source_timestamp_counts = {"present": 0, "missing": 0,
                                         "invalid": 0}
        self._lifecycle_events = 0
        self._last_params = None
        self._run_id = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ")
        self._writers = {
            name: BoundedJsonlWriter(
                self.output_dir, name, max_rows=max_file_rows,
                max_bytes=max_file_bytes, retention_hours=retention_hours,
                max_files=max_files, preserve_files=self.preserve_raw,
                run_id=self._run_id)
            for name in ("quotes", "signals", "decay", "lifecycle")
        }
        self.rolling_provider = rolling_provider
        if self.rolling_provider is None and rolling_csv:
            self.rolling_provider = LegacyRollingStrategy(
                csv_path=rolling_csv, market_key=self.market,
                sell_quantile=sell_quantile, buy_quantile=buy_quantile,
                fees_bps=fees_bps, min_samples=10)
        self.rolling_csv = rolling_csv
        self.sell_quantile = sell_quantile
        self.buy_quantile = buy_quantile
        self.fees_bps = fees_bps

    def _enqueue(self, item, *, kind: str) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            if kind == "update":
                self.dropped_updates += 1
            else:
                self.dropped_lifecycle += 1

    def _on_update(self, venue: str, kind: str, message: dict,
                   received_wall_ns: int, received_mono_ns: int,
                   connection_id: int) -> None:
        book = self.books.get(venue)
        if book is None:
            return
        quote = normalize_feed_update(
            venue, kind, message, received_wall_ns, received_mono_ns,
            connection_id, book, self.depth_levels)
        self._enqueue(("quote", quote, message), kind="update")

    def _on_lifecycle(self, venue: str, event: str, received_wall_ns: int,
                      received_mono_ns: int, connection_id: int) -> None:
        self._enqueue(("lifecycle", {
            "record_type": "lifecycle", "venue": venue, "event": event,
            "received_wall_ns": received_wall_ns,
            "received_mono_ns": received_mono_ns,
            "connection_id": connection_id,
        }), kind="lifecycle")

    def _params(self, received_wall_ns: int):
        if self.rolling_provider is None:
            return SimpleNamespace(valid=False,
                                   invalid_reason="rolling_csv_not_configured")
        try:
            params = self.rolling_provider.refresh(received_wall_ns / 1e9)
        except Exception as exc:
            log.warning("rolling parameter refresh failed: %s", exc)
            params = SimpleNamespace(
                valid=False,
                invalid_reason=f"rolling_provider_error:{type(exc).__name__}")
        self._last_params = params
        return params

    @staticmethod
    def _quote_record(quote: FeedQuote, message: dict, params: object) -> dict:
        source = quote.source_timestamp
        row = {
            "record_type": "quote", "venue": quote.venue,
            "message_kind": quote.kind,
            "channel": message.get("channel"),
            "message_type": message.get("type"),
            # Bounded normalized depth and timing replace unbounded raw frames.
            "received_wall_ns": quote.received_wall_ns,
            "received_mono_ns": quote.received_mono_ns,
            "connection_id": quote.connection_id,
            "source_timestamp_status": source.status,
            "source_timestamp_raw": source.raw_value,
            "source_timestamp_field": source.field,
            "source_timestamp_unit": source.unit,
            "source_timestamp_ns": source.source_timestamp_ns,
            "source_age_ms": quote.source_age_ms,
            "bids": [[px, sz] for px, sz in quote.bids],
            "asks": [[px, sz] for px, sz in quote.asks],
        }
        row.update(_parameter_fields(params))
        return row

    def _write_edge_records(self, records: list[dict]) -> None:
        for row in records:
            writer_name = {"signal": "signals", "decay": "decay"}.get(
                row.get("record_type"))
            target = self._writers.get(writer_name) if writer_name else None
            if target is not None:
                target.append(row)

    def _process(self, item) -> None:
        kind = item[0]
        if kind == "lifecycle":
            self._writers["lifecycle"].append(item[1])
            self._lifecycle_events += 1
            return
        _, quote, message = item
        params = self._params(quote.received_wall_ns)
        self._writers["quotes"].append(
            self._quote_record(quote, message, params))
        self._source_timestamp_counts[quote.source_timestamp.status] += 1
        self._write_edge_records(self.tracker.ingest(quote, params))

    def _make_feeds(self):
        if self._feed_factory is not None:
            return self._feed_factory(self._on_update, self._on_lifecycle,
                                      self.books)
        return [
            HLBookFeed("entropy", self.hl_ws_url, self.hl_coin,
                       self.books["entropy"], lambda: None,
                       on_update=self._on_update,
                       on_lifecycle=self._on_lifecycle),
            LighterBookFeed("lighter-rh", self.rh_ws_url, self.rh_market_id,
                            self.books["lighter-rh"], lambda: None,
                            on_update=self._on_update,
                            on_lifecycle=self._on_lifecycle),
        ]

    async def _stop_after(self, stop: asyncio.Event, duration_sec: float):
        await asyncio.sleep(duration_sec)
        stop.set()

    def _manifest(self, started_ns: int, ended_ns: int) -> dict:
        return {
            "schema_version": 1,
            "record_only": True,
            "market": self.market,
            "symbol": self.symbol,
            "hedge": self.hedge,
            "market_ids": {"entropy": self.hl_coin,
                            "lighter-rh": self.rh_market_id},
            "public_endpoints": {"entropy_ws": self.hl_ws_url,
                                  "lighter_rh_ws": self.rh_ws_url},
            "depth_levels": self.depth_levels,
            "pair_max_skew_ms": self.pair_max_skew_ms,
            "decay_horizons_ms": list(EdgeDecayTracker.HORIZONS_MS),
            "rolling_csv": self.rolling_csv or None,
            "rolling_quantiles": {"sell": self.sell_quantile,
                                  "buy": self.buy_quantile},
            "fees_bps_for_rolling": self.fees_bps,
            "retention_hours": self.retention_hours,
            "max_file_rows": self.max_file_rows,
            "max_file_bytes": self.max_file_bytes,
            "max_files": self.max_files,
            "preserve_raw": self.preserve_raw,
            "started_wall_ns": started_ns,
            "ended_wall_ns": ended_ns,
            "source_timestamp_counts": self._source_timestamp_counts,
            "lifecycle_events": self._lifecycle_events,
            "dropped_updates": self.dropped_updates,
            "dropped_lifecycle": self.dropped_lifecycle,
            "pending_decay_horizons": self.tracker.pending_count,
            "writer_stats": {name: writer.stats
                             for name, writer in self._writers.items()},
        }

    async def run(self, duration_sec: float) -> dict:
        if duration_sec <= 0 or duration_sec > 3600:
            raise ValueError("duration_sec must be in (0, 3600]")
        # The recorder is constructed by a synchronous CLI before
        # ``asyncio.run`` creates its loop.  Rebind the queue here so Python
        # 3.9+ never awaits a queue attached to a previous loop.
        self._queue = asyncio.Queue(maxsize=self.queue_size)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = self.output_dir / "manifest.json"
        if manifest_path.exists():
            raise FileExistsError(f"refusing to overwrite {manifest_path}")
        started_ns = time.time_ns()
        stop = asyncio.Event()
        feeds = self._make_feeds()
        tasks = [asyncio.create_task(feed.run(stop),
                                     name=f"quote-feed-{idx}")
                 for idx, feed in enumerate(feeds)]
        timer = asyncio.create_task(self._stop_after(stop, duration_sec),
                                    name="quote-capture-timer")
        try:
            while not stop.is_set() or not self._queue.empty():
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                try:
                    self._process(item)
                finally:
                    self._queue.task_done()
        finally:
            stop.set()
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            done, pending = await asyncio.wait(tasks, timeout=0.5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            while not self._queue.empty():
                self._process(self._queue.get_nowait())
                self._queue.task_done()
            self._write_edge_records(
                self.tracker.flush(time.time_ns(),
                                   params=self._last_params, final=True))
            for writer in self._writers.values():
                writer.close()
        ended_ns = time.time_ns()
        manifest = self._manifest(started_ns, ended_ns)
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n",
                                 encoding="utf-8")
        return manifest
