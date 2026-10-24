"""Websocket order-book feeds, writing into entropy_arb.book.OrderBook.

Two protocols, one per exchange family:

LighterBookFeed: zkLighter order_book channel (snapshot + diffs, server
    pings, diff-nonce gap detection — a gapped book is dropped and
    resubscribed rather than traded as a fiction).
HLBookFeed: the official Hyperliquid websocket (wss://api.hyperliquid.xyz/ws)
    l2Book channel with fast snapshots and client app-pings. Every price this
    bot trades on comes straight from the exchange that will fill the order.

Both touch the book on any inbound frame (connection-based freshness: a quiet
market is not stale, only a dead feed is) and reconnect with backoff.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Optional

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook

log = logging.getLogger("feeds")

FeedUpdateCallback = Callable[[str, str, dict, int, int, int], None]
FeedLifecycleCallback = Callable[[str, str, int, int, int], None]


def _chan_id(channel: str) -> Optional[int]:
    """'order_book:32' / 'order_book/32' -> 32."""
    for sep in (":", "/"):
        if sep in channel:
            try:
                return int(channel.rsplit(sep, 1)[1])
            except ValueError:
                return None
    return None


class LighterBookFeed:
    """zkLighter order book for one market over one connection."""

    def __init__(self, name: str, ws_url: str, market_id: int, book: OrderBook,
                 notify: Callable[[], None], *,
                 on_update: Optional[FeedUpdateCallback] = None,
                 on_lifecycle: Optional[FeedLifecycleCallback] = None) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.book = book
        self.notify = notify
        self.on_update = on_update
        self.on_lifecycle = on_lifecycle
        self._nonce: Optional[int] = None
        self._synced = False
        self._connection_id = 0

    def _emit_lifecycle(self, event: str) -> None:
        if self.on_lifecycle is None:
            return
        try:
            self.on_lifecycle(self.name, event, time.time_ns(),
                              time.monotonic_ns(), self._connection_id)
        except Exception:
            log.exception("[%s] lifecycle callback failed", self.name)

    def _emit_update(self, kind: str, msg: dict,
                     received_wall_ns: Optional[int],
                     received_mono_ns: Optional[int]) -> None:
        if self.on_update is None:
            return
        if received_wall_ns is None:
            received_wall_ns = time.time_ns()
        if received_mono_ns is None:
            received_mono_ns = time.monotonic_ns()
        try:
            self.on_update(self.name, kind, msg, received_wall_ns,
                           received_mono_ns, self._connection_id)
        except Exception:
            log.exception("[%s] update callback failed", self.name)

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({"type": "subscribe",
                                  "channel": f"order_book/{self.market_id}"}))

    async def _handle_book(self, ws, msg: dict, snapshot: bool,
                           received_wall_ns: Optional[int] = None,
                           received_mono_ns: Optional[int] = None) -> None:
        if _chan_id(msg.get("channel", "")) != self.market_id:
            return
        ob = msg["order_book"]
        if snapshot:
            self._nonce = ob.get("nonce")
            self._synced = True
            self.book.apply_lighter(ob, snapshot=True)
            log.info("[%s] snapshot: %d bids / %d asks", self.name,
                     len(self.book.bids), len(self.book.asks))
            self._emit_update("snapshot", msg, received_wall_ns,
                              received_mono_ns)
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
            self._emit_lifecycle("book_gap")
            self.notify()
            await ws.send(json.dumps({"type": "unsubscribe",
                                      "channel": f"order_book/{self.market_id}"}))
            await self._subscribe(ws)
            return
        if end is not None:
            self._nonce = end
        self.book.apply_lighter(ob, snapshot=False)
        self._emit_update("diff", msg, received_wall_ns, received_mono_ns)
        self.notify()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    self._connection_id += 1
                    self._emit_lifecycle("connected")
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    self.book.clear()
                    self._nonce = None
                    self._synced = False
                    async for raw in ws:
                        backoff = 1.0
                        received_wall_ns = time.time_ns()
                        received_mono_ns = time.monotonic_ns()
                        msg = json.loads(raw)
                        t = msg.get("type")
                        self.book.touch()
                        if t == "update/order_book":
                            await self._handle_book(
                                ws, msg, snapshot=False,
                                received_wall_ns=received_wall_ns,
                                received_mono_ns=received_mono_ns)
                        elif t == "subscribed/order_book":
                            await self._handle_book(
                                ws, msg, snapshot=True,
                                received_wall_ns=received_wall_ns,
                                received_mono_ns=received_mono_ns)
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
            self._emit_lifecycle("disconnected")
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class HLBookFeed:
    """Official Hyperliquid l2Book consumer for one coin (e.g. 'io:SNDK')."""

    def __init__(self, name: str, ws_url: str, coin: str, book: OrderBook,
                 notify: Callable[[], None], ping_sec: float = 5.0, *,
                 on_update: Optional[FeedUpdateCallback] = None,
                 on_lifecycle: Optional[FeedLifecycleCallback] = None) -> None:
        self.name = name
        self.ws_url = ws_url
        self.coin = coin
        self.book = book
        self.notify = notify
        self.on_update = on_update
        self.on_lifecycle = on_lifecycle
        self.ping_sec = ping_sec
        self._snapped = False
        self._connection_id = 0

    def _emit_lifecycle(self, event: str) -> None:
        if self.on_lifecycle is None:
            return
        try:
            self.on_lifecycle(self.name, event, time.time_ns(),
                              time.monotonic_ns(), self._connection_id)
        except Exception:
            log.exception("[%s] lifecycle callback failed", self.name)

    def _emit_update(self, kind: str, msg: dict,
                     received_wall_ns: Optional[int],
                     received_mono_ns: Optional[int]) -> None:
        if self.on_update is None:
            return
        if received_wall_ns is None:
            received_wall_ns = time.time_ns()
        if received_mono_ns is None:
            received_mono_ns = time.monotonic_ns()
        try:
            self.on_update(self.name, kind, msg, received_wall_ns,
                           received_mono_ns, self._connection_id)
        except Exception:
            log.exception("[%s] update callback failed", self.name)

    def _on_frame(self, msg: dict, received_wall_ns: Optional[int] = None,
                  received_mono_ns: Optional[int] = None) -> None:
        self.book.touch()
        if msg.get("channel") == "l2Book":
            d = msg.get("data") or {}
            if d.get("coin") == self.coin:
                self.book.apply_hl(d["levels"])
                if not self._snapped:
                    self._snapped = True
                    log.info("[%s] snapshot: %d bids / %d asks", self.name,
                             len(self.book.bids), len(self.book.asks))
                self._emit_update("snapshot", msg, received_wall_ns,
                                  received_mono_ns)
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
                    self._connection_id += 1
                    self._emit_lifecycle("connected")
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
                        self._on_frame(
                            json.loads(raw), time.time_ns(), time.monotonic_ns())
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
            self._emit_lifecycle("disconnected")
            self.book.ready = False
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
