"""Automatic 1-minute orderbook data recorder.

While the bot runs (live or --record-only), both venues' actual order books
are sampled once per second. Each valid BBO sample is persisted for persistence
research and aggregated into one CSV row per minute. These are the datasets
users analyze (tools/analyze.py) to choose thresholds.midline_bps /
upper_bps / lower_bps and execution.premium_persist_sec for config.yaml.

Definitions (all in bps, fees NOT included — the engine adds fees on top):

    premium    = (entropy_mid / hedge_mid - 1) * 1e4
                 the mid-to-mid premium of Entropy over the hedge venue;
                 its long-run center is what midline_bps hardcodes.
    sell_edge  = (entropy_bid / hedge_ask - 1) * 1e4
                 the EXECUTABLE premium for SELL-entropy/BUY-hedge; the
                 engine fires this direction when sell_edge clears
                 midline_bps + upper_bps (plus fees).
    buy_edge   = (hedge_bid / entropy_ask - 1) * 1e4
                 the executable premium for BUY-entropy/SELL-hedge; fires
                 when buy_edge clears lower_bps - midline_bps (plus fees).

Bid/ask columns are the minute's last fresh sample (close). A row is only
written for minutes with at least one sample where both books were fresh;
`samples` says how many of the ~60 seconds qualified.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Optional

from .book import OrderBook
from .premium import calculate_premiums

log = logging.getLogger("recorder")

HEADER = ["minute_ts", "time_utc", "symbol", "hedge",
          "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
          "premium_open_bps", "premium_high_bps", "premium_low_bps",
          "premium_close_bps", "premium_mean_bps", "premium_std_bps",
          "sell_edge_mean_bps", "sell_edge_max_bps",
          "buy_edge_mean_bps", "buy_edge_max_bps", "samples"]
SAMPLE_HEADER = ["timestamp_ms", "premium_bps",
                 "sell_edge_bps", "buy_edge_bps",
                 "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
                 "entropy_book_update_ms", "hedge_book_update_ms"]
SAMPLE_FLUSH_ROWS = 10


def _samples_path(minutes_path: str) -> str:
    directory, filename = os.path.split(minutes_path)
    stem, ext = os.path.splitext(filename)
    if stem == "minutes":
        sample_stem = "samples-v2"
    elif stem.startswith("minutes-"):
        sample_stem = f"samples-v2-{stem[len('minutes-'):]}"
    else:
        sample_stem = f"{stem}-samples-v2"
    return os.path.join(directory, sample_stem + ext)


class _MinuteAgg:
    __slots__ = ("minute", "n", "p_open", "p_high", "p_low", "p_close",
                 "p_sum", "p_sumsq", "s_sum", "s_max", "b_sum", "b_max",
                 "e_bid", "e_ask", "h_bid", "h_ask")

    def __init__(self, minute: int) -> None:
        self.minute = minute
        self.n = 0
        self.p_open = self.p_high = self.p_low = self.p_close = 0.0
        self.p_sum = self.p_sumsq = 0.0
        self.s_sum = 0.0
        self.s_max = -math.inf
        self.b_sum = 0.0
        self.b_max = -math.inf
        self.e_bid = self.e_ask = self.h_bid = self.h_ask = 0.0

    def add(self, e_bid: float, e_ask: float, h_bid: float,
            h_ask: float) -> tuple[float, float, float]:
        values = calculate_premiums(e_bid, e_ask, h_bid, h_ask)
        prem = values.premium_bps
        sell_edge = values.sell_edge_bps
        buy_edge = values.buy_edge_bps
        if self.n == 0:
            self.p_open = self.p_high = self.p_low = prem
        self.n += 1
        self.p_high = max(self.p_high, prem)
        self.p_low = min(self.p_low, prem)
        self.p_close = prem
        self.p_sum += prem
        self.p_sumsq += prem * prem
        self.s_sum += sell_edge
        self.s_max = max(self.s_max, sell_edge)
        self.b_sum += buy_edge
        self.b_max = max(self.b_max, buy_edge)
        self.e_bid, self.e_ask, self.h_bid, self.h_ask = e_bid, e_ask, h_bid, h_ask
        return prem, sell_edge, buy_edge

    def row(self, symbol: str, hedge: str) -> list:
        mean = self.p_sum / self.n
        var = max(self.p_sumsq / self.n - mean * mean, 0.0)
        ts = self.minute * 60
        return [ts,
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                symbol, hedge,
                f"{self.e_bid:.10g}", f"{self.e_ask:.10g}",
                f"{self.h_bid:.10g}", f"{self.h_ask:.10g}",
                f"{self.p_open:.3f}", f"{self.p_high:.3f}",
                f"{self.p_low:.3f}", f"{self.p_close:.3f}",
                f"{mean:.3f}", f"{math.sqrt(var):.3f}",
                f"{self.s_sum / self.n:.3f}", f"{self.s_max:.3f}",
                f"{self.b_sum / self.n:.3f}", f"{self.b_max:.3f}",
                self.n]


class MinuteRecorder:
    def __init__(self, path: str, entropy_book: OrderBook, hedge_book: OrderBook,
                 staleness_sec: float, interval_sec: float = 1.0, *,
                 symbol: str, hedge: str) -> None:
        self.path = path
        self.samples_path = _samples_path(path)
        self.symbol = symbol
        self.hedge = hedge
        self.entropy_book = entropy_book
        self.hedge_book = hedge_book
        self.staleness_sec = staleness_sec
        self.interval_sec = interval_sec
        self.rows_written = 0
        self._agg: Optional[_MinuteAgg] = None
        self._fh = None
        self._writer = None
        self._samples_fh = None
        self._samples_writer = None
        self._samples_pending = 0

    def _open(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            # never append rows under a different schema's header
            with open(self.path) as fh0:
                if fh0.readline().strip() != ",".join(HEADER):
                    log.warning("%s has an old header — rotated to %s.old",
                                self.path, self.path)
                    os.replace(self.path, self.path + ".old")
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._fh = open(self.path, "a", newline="")
        self._writer = csv.writer(self._fh)
        if new:
            self._writer.writerow(HEADER)
            self._fh.flush()
        log.info("recording 1-minute orderbook data -> %s", self.path)

    def _open_samples(self) -> None:
        d = os.path.dirname(self.samples_path)
        if d:
            os.makedirs(d, exist_ok=True)
        if (os.path.exists(self.samples_path)
                and os.path.getsize(self.samples_path) > 0):
            with open(self.samples_path) as fh0:
                if fh0.readline().strip() != ",".join(SAMPLE_HEADER):
                    log.warning("%s has an old header — rotated to %s.old",
                                self.samples_path, self.samples_path)
                    os.replace(self.samples_path, self.samples_path + ".old")
        new = (not os.path.exists(self.samples_path)
               or os.path.getsize(self.samples_path) == 0)
        self._samples_fh = open(self.samples_path, "a", newline="")
        self._samples_writer = csv.writer(self._samples_fh)
        if new:
            self._samples_writer.writerow(SAMPLE_HEADER)
            self._samples_fh.flush()
        log.info("recording 1-second edge data -> %s", self.samples_path)

    def _write_sample(self, timestamp_ms: int, premium_bps: float,
                      sell_edge_bps: float, buy_edge_bps: float,
                      e_bid: float, e_ask: float, h_bid: float, h_ask: float,
                      e_update_ms: int, h_update_ms: int) -> None:
        if self._samples_writer is None:
            self._open_samples()
        self._samples_writer.writerow([
            timestamp_ms, premium_bps, sell_edge_bps, buy_edge_bps,
            e_bid, e_ask, h_bid, h_ask, e_update_ms, h_update_ms,
        ])
        self._samples_pending += 1
        if self._samples_pending >= SAMPLE_FLUSH_ROWS:
            self._samples_fh.flush()
            self._samples_pending = 0

    def _flush_agg(self) -> None:
        if self._agg is None or self._agg.n == 0:
            self._agg = None
            return
        if self._writer is None:
            self._open()
        self._writer.writerow(self._agg.row(self.symbol, self.hedge))
        self._fh.flush()
        self.rows_written += 1
        self._agg = None

    def sample(self, now: Optional[float] = None) -> None:
        """Take one sample; call ~1/sec. Rolls the minute over as needed."""
        now = time.time() if now is None else now
        minute = int(now // 60)
        if self._agg is not None and self._agg.minute != minute:
            self._flush_agg()
        if not (self.entropy_book.is_fresh(self.staleness_sec)
                and self.hedge_book.is_fresh(self.staleness_sec)):
            return
        e_bid, e_ask = self.entropy_book.best_bid(), self.entropy_book.best_ask()
        h_bid, h_ask = self.hedge_book.best_bid(), self.hedge_book.best_ask()
        if None in (e_bid, e_ask, h_bid, h_ask):
            return
        if self._agg is None:
            self._agg = _MinuteAgg(minute)
        premium, sell_edge, buy_edge = self._agg.add(
            e_bid, e_ask, h_bid, h_ask)
        self._write_sample(
            int(now * 1000), premium, sell_edge, buy_edge,
            e_bid, e_ask, h_bid, h_ask,
            int(self.entropy_book.last_update_ts * 1000),
            int(self.hedge_book.last_update_ts * 1000),
        )

    def close(self) -> None:
        """Flush the partial minute and close the file (call on shutdown)."""
        self._flush_agg()
        if self._fh is not None:
            self._fh.close()
            self._fh = self._writer = None
        if self._samples_fh is not None:
            self._samples_fh.flush()
            self._samples_fh.close()
            self._samples_fh = self._samples_writer = None
            self._samples_pending = 0

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    self.sample()
                except Exception:
                    log.exception("recorder sample failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.close()
            log.info("recorder stopped — %d minute row(s) written to %s",
                     self.rows_written, self.path)
