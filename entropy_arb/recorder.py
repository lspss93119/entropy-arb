"""Automatic 1-minute orderbook data recorder.

While the bot runs (live or --record-only), both venues' actual order books
are sampled once per second and aggregated into one CSV row per minute.
This is the dataset users analyze (tools/analyze.py) to choose
thresholds.midline_bps / upper_bps / lower_bps for config.yaml.

Definitions (all in bps, fees NOT included — the engine adds fees on top):

    premium    = (A_mid / B_mid - 1) * 1e4
                 the mid-to-mid premium of Venue A over Venue B;
                 its long-run center is what midline_bps hardcodes.
    sell_a_edge = (A_bid / B_ask - 1) * 1e4
                 the EXECUTABLE premium for SELL-A/BUY-B; the engine fires
                 this direction when it clears midline_bps + upper_bps (plus fees).
    buy_a_edge  = (B_bid / A_ask - 1) * 1e4
                 the executable premium for BUY-A/SELL-B; fires when it
                 clears lower_bps - midline_bps (plus fees).

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

log = logging.getLogger("recorder")

HEADER = ["minute_ts", "time_utc", "venue_a", "venue_b", "symbol",
          "a_bid", "a_ask", "b_bid", "b_ask",
          "premium_open_bps", "premium_high_bps", "premium_low_bps",
          "premium_close_bps", "premium_mean_bps", "premium_std_bps",
          "sell_a_edge_mean_bps", "sell_a_edge_max_bps",
          "buy_a_edge_mean_bps", "buy_a_edge_max_bps", "samples"]


class _MinuteAgg:
    __slots__ = ("minute", "n", "p_open", "p_high", "p_low", "p_close",
                 "p_sum", "p_sumsq", "s_sum", "s_max", "b_sum", "b_max",
                 "venue_a", "venue_b", "symbol",
                 "a_bid", "a_ask", "b_bid", "b_ask")

    def __init__(self, minute: int, venue_a: str, venue_b: str,
                 symbol: str) -> None:
        self.minute = minute
        self.venue_a, self.venue_b, self.symbol = venue_a, venue_b, symbol
        self.n = 0
        self.p_open = self.p_high = self.p_low = self.p_close = 0.0
        self.p_sum = self.p_sumsq = 0.0
        self.s_sum = 0.0
        self.s_max = -math.inf
        self.b_sum = 0.0
        self.b_max = -math.inf
        self.a_bid = self.a_ask = self.b_bid = self.b_ask = 0.0

    def add(self, a_bid: float, a_ask: float, b_bid: float, b_ask: float) -> None:
        a_mid = (a_bid + a_ask) / 2.0
        b_mid = (b_bid + b_ask) / 2.0
        prem = (a_mid / b_mid - 1.0) * 1e4
        sell_edge = (a_bid / b_ask - 1.0) * 1e4
        buy_edge = (b_bid / a_ask - 1.0) * 1e4
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
        self.a_bid, self.a_ask, self.b_bid, self.b_ask = a_bid, a_ask, b_bid, b_ask

    def row(self) -> list:
        mean = self.p_sum / self.n
        var = max(self.p_sumsq / self.n - mean * mean, 0.0)
        ts = self.minute * 60
        return [ts,
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                self.venue_a, self.venue_b, self.symbol,
                f"{self.a_bid:.10g}", f"{self.a_ask:.10g}",
                f"{self.b_bid:.10g}", f"{self.b_ask:.10g}",
                f"{self.p_open:.3f}", f"{self.p_high:.3f}",
                f"{self.p_low:.3f}", f"{self.p_close:.3f}",
                f"{mean:.3f}", f"{math.sqrt(var):.3f}",
                f"{self.s_sum / self.n:.3f}", f"{self.s_max:.3f}",
                f"{self.b_sum / self.n:.3f}", f"{self.b_max:.3f}",
                self.n]


class MinuteRecorder:
    def __init__(self, path: str, venue_a_book: OrderBook,
                 venue_b_book: OrderBook, staleness_sec: float,
                 interval_sec: float = 1.0, *, venue_a_name: str,
                 venue_b_name: str, symbol: str) -> None:
        self.path = path
        self.venue_a_book = venue_a_book
        self.venue_b_book = venue_b_book
        self.venue_a_name = venue_a_name
        self.venue_b_name = venue_b_name
        self.symbol = symbol
        self.staleness_sec = staleness_sec
        self.interval_sec = interval_sec
        self.rows_written = 0
        self._agg: Optional[_MinuteAgg] = None
        self._fh = None
        self._writer = None

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

    def _flush_agg(self) -> None:
        if self._agg is None or self._agg.n == 0:
            self._agg = None
            return
        if self._writer is None:
            self._open()
        self._writer.writerow(self._agg.row())
        self._fh.flush()
        self.rows_written += 1
        self._agg = None

    def sample(self, now: Optional[float] = None) -> None:
        """Take one sample; call ~1/sec. Rolls the minute over as needed."""
        now = time.time() if now is None else now
        minute = int(now // 60)
        if self._agg is not None and self._agg.minute != minute:
            self._flush_agg()
        if not (self.venue_a_book.is_fresh(self.staleness_sec)
                and self.venue_b_book.is_fresh(self.staleness_sec)):
            return
        a_bid, a_ask = self.venue_a_book.best_bid(), self.venue_a_book.best_ask()
        b_bid, b_ask = self.venue_b_book.best_bid(), self.venue_b_book.best_ask()
        if None in (a_bid, a_ask, b_bid, b_ask):
            return
        if self._agg is None:
            self._agg = _MinuteAgg(minute, self.venue_a_name,
                                   self.venue_b_name, self.symbol)
        self._agg.add(a_bid, a_ask, b_bid, b_ask)

    def close(self) -> None:
        """Flush the partial minute and close the file (call on shutdown)."""
        self._flush_agg()
        if self._fh is not None:
            self._fh.close()
            self._fh = self._writer = None

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
