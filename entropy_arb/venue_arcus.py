"""Arcus public market-data venue.

This adapter is intentionally read-only.  Arcus can be selected for
``--record-only`` market-data collection, but authenticated account access and
order execution are not implemented in this phase.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional

import aiohttp

from .book import OrderBook
from .config import VenueConf
from .feeds import ArcusBBOFeed

log = logging.getLogger("arcus")

REST_TIMEOUT = 10.0


def _decimal(value, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(f"[ARCUS] invalid {field}: {value!r}") from exc
    if not result.is_finite() or result <= 0:
        raise RuntimeError(f"[ARCUS] invalid {field}: {value!r}")
    return result


def _decimal_places(value: Decimal) -> int:
    return max(0, -value.as_tuple().exponent)


class ArcusVenue:
    """Arcus market metadata and public BBO stream for one symbol."""

    kind = "arcus"

    def __init__(self, conf: VenueConf, api_url: str, ws_url: str,
                 session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.venue_name = conf.venue_name
        self.api_url = api_url.rstrip("/")
        self.ws_url = ws_url
        self.session = session
        self.settle_timeout = settle_timeout_sec

        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0
        self.equity = None
        self.free = None
        self.start_equity = None
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0

        self.market_id = -1
        self.market_display_name = ""
        self.market: Optional[dict] = None
        self.tick_size = Decimal("0")
        self.step_size = Decimal("0")
        self.price_decimals = 0
        self.size_decimals = 0
        self.min_base = 0.0
        self.min_quote = 0.0
        self.max_order_size = 0.0

    async def _get(self, path: str, params: Optional[dict] = None):
        async with self.session.get(
                self.api_url + path, params=params,
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as response:
            response.raise_for_status()
            return await response.json()

    async def load_market(self) -> None:
        payload = await self._get("/v1/markets")
        markets = payload.get("markets") if isinstance(payload, dict) else None
        if not isinstance(markets, list):
            raise RuntimeError("[ARCUS] /v1/markets response has no markets list")

        symbol = self.conf.symbol.upper()
        matches = [market for market in markets
                   if isinstance(market, dict)
                   and str(market.get("baseAsset", "")).upper() == symbol]
        if not matches:
            raise RuntimeError(f"[ARCUS] canonical symbol {self.conf.symbol!r} "
                               "not found in markets")
        if len(matches) != 1:
            identifiers = [m.get("marketDisplayName") for m in matches]
            raise RuntimeError(f"[ARCUS] canonical symbol {self.conf.symbol!r} "
                               f"is ambiguous: {identifiers}")

        market = matches[0]
        if str(market.get("type", "")).upper() != "PERPETUAL":
            raise RuntimeError(f"[ARCUS] {market.get('marketDisplayName', symbol)} "
                               "is not perpetual")
        if str(market.get("status", "")).upper() != "ONLINE":
            raise RuntimeError(f"[ARCUS] {market.get('marketDisplayName', symbol)} "
                               "is not online")
        display_name = market.get("marketDisplayName")
        if not isinstance(display_name, str) or not display_name:
            raise RuntimeError("[ARCUS] marketDisplayName is missing")
        try:
            market_id = int(market["marketId"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("[ARCUS] marketId is missing or invalid") from exc

        tick_size = _decimal(market.get("tickSize"), "tickSize")
        step_size = _decimal(market.get("stepSize"), "stepSize")
        min_order_size = _decimal(market.get("minOrderSize"), "minOrderSize")
        min_order_notional = _decimal(market.get("minOrderNotional"),
                                       "minOrderNotional")
        max_order_size = _decimal(market.get("maxOrderSize"), "maxOrderSize")

        self.market = market
        self.market_id = market_id
        self.market_display_name = display_name
        self.tick_size = tick_size
        self.step_size = step_size
        self.price_decimals = _decimal_places(tick_size)
        self.size_decimals = _decimal_places(step_size)
        self.min_base = float(min_order_size)
        self.min_quote = float(min_order_notional)
        self.max_order_size = float(max_order_size)
        log.info("[%s] %s market_id=%d px_dec=%d sz_dec=%d min_base=%s "
                 "min_quote=%s", self.name, self.market_display_name,
                 self.market_id, self.price_decimals, self.size_decimals,
                 min_order_size, min_order_notional)

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        if live:
            raise RuntimeError("Arcus live execution not implemented; use "
                               "--record-only")
        if not self.market_display_name:
            raise RuntimeError("[ARCUS] load_market must complete before "
                               "starting the BBO feed")
        return [asyncio.create_task(
            ArcusBBOFeed(self.name, self.ws_url, self.market_display_name,
                         self.book, notify).run(stop),
            name=f"book-{self.key}")]

    def ready_to_trade(self) -> bool:
        """Expose market-data readiness; execution is blocked at startup."""
        return self.book.ready

    async def warm_http(self) -> None:
        """No authenticated/order HTTP path exists for Arcus in this phase."""
        return None

    async def close(self) -> None:
        return None
