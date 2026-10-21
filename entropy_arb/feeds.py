"""Websocket order-book feeds, writing into entropy_arb.book.OrderBook.

Two protocols, one per exchange family:

LighterBookFeed: zkLighter order_book channel (snapshot + diffs, server
    pings, diff-nonce gap detection — a gapped book is dropped and
    resubscribed rather than traded as a fiction).
HLBookFeed: the official Hyperliquid websocket (wss://api.hyperliquid.xyz/ws)
    l2Book channel with fast snapshots and client app-pings. Every price this
    bot trades on comes straight from the exchange that will fill the order.

Both retain connection liveness separately from book event timestamps.
Reconnects use backoff; quote-age checking is an optional decision gate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Callable, Optional

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook

log = logging.getLogger("feeds")


def _chan_id(channel: str) -> Optional[int]:
    """'order_book:32' / 'order_book/32' -> 32."""
    for sep in (":", "/"):
        if sep in channel:
            try:
                return int(channel.rsplit(sep, 1)[1])
            except ValueError:
                return None
    return None


def _source_time_seconds(value, *, divisor: float) -> Optional[float]:
    """Normalize documented venue event time (milliseconds/microseconds)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(stamp) or stamp <= 0:
        return None
    return stamp / divisor


class LighterBookFeed:
    """zkLighter order book for one market over one connection."""

    def __init__(self, name: str, ws_url: str, market_id: int, book: OrderBook,
                 notify: Callable[[], None]) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.book = book
        self.notify = notify
        self._nonce: Optional[int] = None
        self._synced = False

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({"type": "subscribe",
                                  "channel": f"order_book/{self.market_id}"}))

    async def _handle_book(self, ws, msg: dict, snapshot: bool) -> None:
        if _chan_id(msg.get("channel", "")) != self.market_id:
            return
        ob = msg["order_book"]
        # Prefer matching-engine time (microseconds) over broadcast time (ms).
        source_us = ob.get("last_updated_at", msg.get("last_updated_at"))
        source_ts = (_source_time_seconds(source_us, divisor=1_000_000.0)
                     if source_us is not None else
                     _source_time_seconds(msg.get("timestamp"), divisor=1000.0))
        if snapshot:
            self._nonce = ob.get("nonce")
            self._synced = True
            self.book.apply_lighter(ob, snapshot=True, source_ts=source_ts)
            log.info("[%s] snapshot: %d bids / %d asks", self.name,
                     len(self.book.bids), len(self.book.asks))
            self.notify()
            return
        # diff: a skipped nonce means we lost a level update — the book is now
        # a fiction. Drop it and resubscribe rather than quote off a ghost.
        if not self._synced:
            return  # no snapshot yet (fresh connection, or one pending after a gap)
        prev, begin, end = self._nonce, ob.get("begin_nonce"), ob.get("nonce")
        if prev is not None and begin is not None and begin > prev + 1:
            log.warning("[%s] diff gap (had %s, got %s) — resubscribing",
                        self.name, prev, begin)
            self._nonce = None
            self._synced = False
            self.book.clear()
            self.notify()
            await ws.send(json.dumps({"type": "unsubscribe",
                                      "channel": f"order_book/{self.market_id}"}))
            await self._subscribe(ws)
            return
        if end is not None:
            self._nonce = end
        self.book.apply_lighter(ob, snapshot=False, source_ts=source_ts)
        self.notify()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    self.book.clear()
                    self._nonce = None
                    self._synced = False
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        t = msg.get("type")
                        self.book.touch()
                        if t == "update/order_book":
                            await self._handle_book(ws, msg, snapshot=False)
                        elif t == "subscribed/order_book":
                            await self._handle_book(ws, msg, snapshot=True)
                        elif t == "connected":
                            await self._subscribe(ws)
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class HLBookFeed:
    """Official Hyperliquid l2Book consumer for one coin (e.g. 'io:SNDK')."""

    def __init__(self, name: str, ws_url: str, coin: str, book: OrderBook,
                 notify: Callable[[], None], ping_sec: float = 5.0) -> None:
        self.name = name
        self.ws_url = ws_url
        self.coin = coin
        self.book = book
        self.notify = notify
        self.ping_sec = ping_sec
        self._snapped = False

    def _on_frame(self, msg: dict) -> None:
        self.book.touch()
        if msg.get("channel") == "l2Book":
            d = msg.get("data") or {}
            if d.get("coin") == self.coin:
                self.book.apply_hl(
                    d["levels"],
                    source_ts=_source_time_seconds(d.get("time"), divisor=1000.0))
                if not self._snapped:
                    self._snapped = True
                    log.info("[%s] snapshot: %d bids / %d asks", self.name,
                             len(self.book.bids), len(self.book.asks))
                self.notify()

    async def _pinger(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self.ping_sec)
                await ws.send(json.dumps({"method": "ping"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            ptask = None
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    log.info("[%s] connected (official ws, %s)", self.name, self.coin)
                    self.book.clear()
                    self._snapped = False
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": self.coin,
                                         "fast": True}}))
                    ptask = asyncio.create_task(self._pinger(ws))
                    async for raw in ws:
                        backoff = 1.0
                        self._on_frame(json.loads(raw))
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            finally:
                if ptask is not None:
                    ptask.cancel()
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
