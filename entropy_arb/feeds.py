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
        if snapshot:
            self._nonce = ob.get("nonce")
            self._synced = True
            self.book.apply_lighter(ob, snapshot=True)
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
        self.book.apply_lighter(ob, snapshot=False)
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
                self.book.apply_hl(d["levels"])
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


class ArcusBBOFeed:
    """Arcus public BBO stream for one market.

    Arcus sends one real best bid and best ask in each ``subscribed`` or
    ``channel_data`` message.  The feed intentionally subscribes using only
    the documented BBO envelope and never invents depth when a side is absent.
    """

    def __init__(self, name: str, ws_url: str, market: str, book: OrderBook,
                 notify: Callable[[], None]) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market = market
        self.book = book
        self.notify = notify
        self.last_sequence_id: Optional[int] = None
        self.last_global_sequence_id: Optional[int] = None
        self.last_timestamp_us: Optional[int] = None

    def handle_message(self, msg: dict) -> None:
        """Apply one decoded Arcus message, ignoring other channels."""
        if not isinstance(msg, dict):
            raise ValueError("BBO message must be an object")
        if msg.get("type") not in ("subscribed", "channel_data"):
            return
        if msg.get("channel") != "bbo" or msg.get("id") != self.market:
            return
        contents = msg.get("contents")
        if not isinstance(contents, dict):
            raise ValueError("BBO message is missing contents")
        try:
            sequence_id = int(contents["lastSequenceId"])
            global_sequence_id = int(contents["globalSequenceId"])
            timestamp_us = int(contents["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("BBO message has malformed sequence/timestamp") from exc
        if (self.last_sequence_id is not None
                and sequence_id < self.last_sequence_id):
            return
        if (self.last_timestamp_us is not None
                and timestamp_us < self.last_timestamp_us):
            return
        self.book.apply_bbo(contents.get("bestBid"), contents.get("bestAsk"))
        self.last_sequence_id = sequence_id
        self.last_global_sequence_id = global_sequence_id
        self.last_timestamp_us = timestamp_us
        self.notify()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    log.info("[%s] connected (Arcus BBO, %s)", self.name,
                             self.market)
                    self.book.clear()
                    self.last_sequence_id = None
                    self.last_global_sequence_id = None
                    self.last_timestamp_us = None
                    await ws.send(json.dumps({"type": "subscribe",
                                              "channel": "bbo",
                                              "id": self.market}))
                    async for raw in ws:
                        backoff = 1.0
                        self.handle_message(json.loads(raw))
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] Arcus BBO ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            self.book.clear()
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
