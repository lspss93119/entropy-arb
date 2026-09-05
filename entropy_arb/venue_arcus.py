"""Arcus market-data and authenticated account-read adapter.

The authenticated implementation is deliberately limited to account-scoped
reads in this phase. No order or cancel method is present yet; the existing
record-only public BBO path remains unchanged.
"""
from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

import aiohttp

from .book import OrderBook
from .config import ArcusCreds, VenueConf
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


def _finite_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(f"[ARCUS] invalid {field}") from exc
    if not result.is_finite():
        raise RuntimeError(f"[ARCUS] invalid {field}")
    return result


def _decimal_places(value: Decimal) -> int:
    return max(0, -value.as_tuple().exponent)


def normalize_position(position: Mapping[str, Any]) -> float:
    """Normalize an Arcus position to signed base-asset quantity."""
    if not isinstance(position, Mapping):
        raise RuntimeError("[ARCUS] position row is not an object")
    side = str(position.get("side", "")).upper()
    size = _finite_decimal(position.get("size"), "position size")
    if side == "LONG":
        return float(abs(size))
    if side == "SHORT":
        return float(-abs(size))
    if side == "FLAT" or size == 0:
        return 0.0
    raise RuntimeError(f"[ARCUS] unknown position side {side!r}")


def _address(value: str) -> str:
    normalized = value.strip().lower()
    if not normalized.startswith("0x"):
        normalized = "0x" + normalized
    if not re.fullmatch(r"0x[0-9a-f]{40}", normalized):
        raise RuntimeError("[ARCUS] ARCUS_ACCOUNT_ADDRESS must be a 20-byte "
                           "hex address")
    return normalized


class ArcusVenue:
    """Arcus market metadata, public BBO, and account reads for one symbol."""

    kind = "arcus"

    def __init__(self, conf: VenueConf, api_url: str, ws_url: str,
                 session: aiohttp.ClientSession,
                 settle_timeout_sec: float,
                 environment: str = "mainnet") -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.venue_name = conf.venue_name
        self.api_url = api_url.rstrip("/")
        self.ws_url = ws_url
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.environment = environment.lower()
        self.signer = None

        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0
        self.equity = None
        self.free = None
        self.start_equity = None
        self.fee_bps = conf.fee_bps
        self.fee_source = conf.arcus_fee_source
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

    async def _get(self, path: str, params: Optional[dict] = None,
                   headers: Optional[Mapping[str, str]] = None):
        async with self.session.get(
                self.api_url + path, params=params,
                headers=dict(headers) if headers else None,
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as response:
            response.raise_for_status()
            return await response.json()

    def _credentials(self) -> ArcusCreds:
        creds = self.conf.arcus_creds
        if creds is None or not creds.complete:
            raise RuntimeError("[ARCUS] account reads require complete "
                               "testnet credentials in .env")
        if self.environment != "testnet":
            raise RuntimeError("Arcus authenticated access requires "
                               "ARCUS_ENV=testnet")
        if creds.account_index is None or not 0 <= creds.account_index <= 9:
            raise RuntimeError("[ARCUS] ARCUS_ACCOUNT_INDEX must be between "
                               "0 and 9")
        return creds

    def _account_params(self, *, include_index: bool = True) -> dict[str, str]:
        creds = self._credentials()
        params = {"address": _address(creds.account_address or "")}
        if include_index:
            params["accountIndex"] = str(creds.account_index)
        return params

    def _account_headers(self) -> dict[str, str]:
        creds = self._credentials()
        return {"X-API-Key": str(creds.api_key)}

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
        """Expose market-data readiness; engine live execution stays blocked."""
        return self.book.ready

    def init_signer(self) -> None:
        """Load the key only for an explicitly selected Arcus testnet path."""
        if self.environment != "testnet":
            raise RuntimeError("Arcus mainnet execution is not enabled")
        creds = self._credentials()
        from .arcus_signing import ArcusSigner
        self.signer = ArcusSigner.from_private_key(
            str(creds.api_key), str(creds.api_private_key))

    async def fetch_account(self) -> dict:
        payload = await self._get("/v1/account",
                                  params=self._account_params(),
                                  headers=self._account_headers())
        if not isinstance(payload, dict):
            raise RuntimeError("[ARCUS] account response is not an object")
        return payload

    async def fetch_positions(self) -> dict:
        payload = await self._get("/v1/positions",
                                  params=self._account_params(),
                                  headers=self._account_headers())
        positions = payload.get("positions") if isinstance(payload, dict) else None
        if not isinstance(positions, dict):
            raise RuntimeError("[ARCUS] positions response has no positions object")
        return positions

    async def fetch_open_orders(self) -> list[dict]:
        payload = await self._get("/v1/openOrders",
                                  params=self._account_params(),
                                  headers=self._account_headers())
        orders = payload.get("orders") if isinstance(payload, dict) else None
        if not isinstance(orders, list):
            raise RuntimeError("[ARCUS] open-orders response has no orders list")
        return orders

    async def fetch_fills(self) -> list[dict]:
        payload = await self._get("/v1/fills",
                                  params=self._account_params(),
                                  headers=self._account_headers())
        fills = payload.get("fills") if isinstance(payload, dict) else None
        if not isinstance(fills, list):
            raise RuntimeError("[ARCUS] fills response has no fills list")
        return fills

    async def fetch_equity(self):
        payload = await self.fetch_account()
        if "equity" not in payload or "freeCollateral" not in payload:
            raise RuntimeError("[ARCUS] account response lacks equity or "
                               "freeCollateral")
        equity = _finite_decimal(payload["equity"], "account equity")
        free = _finite_decimal(payload["freeCollateral"],
                                "free collateral")
        return float(equity), float(free)

    async def fetch_position(self) -> float:
        if self.market_id < 0:
            raise RuntimeError("[ARCUS] load_market must complete before "
                               "fetching a position")
        positions = await self.fetch_positions()
        row = positions.get(str(self.market_id))
        return normalize_position(row) if row is not None else 0.0

    async def fetch_fee_tier(self) -> dict[str, Any]:
        params = self._account_params(include_index=False)
        params["include"] = "feeTier"
        payload = await self._get("/v1/account/stats", params=params,
                                  headers=self._account_headers())
        tier = payload.get("tradingFeeTier") if isinstance(payload, dict) else None
        if not isinstance(tier, dict):
            raise RuntimeError("[ARCUS] account stats lacks tradingFeeTier")
        maker_ppm = _finite_decimal(tier.get("makerFeePpm"),
                                     "maker fee ppm")
        taker_ppm = _finite_decimal(tier.get("takerFeePpm"),
                                     "taker fee ppm")
        if maker_ppm < 0 or taker_ppm < 0:
            raise RuntimeError("[ARCUS] account fee ppm cannot be negative")
        return {
            "level": int(tier["level"]),
            "maker_fee_bps": float(maker_ppm / Decimal("100")),
            "taker_fee_bps": float(taker_ppm / Decimal("100")),
            "source": "account_api",
        }

    async def refresh_fee(self) -> float:
        """Use account fees only when the YAML explicitly opts in."""
        if self.conf.arcus_fee_source != "account_api":
            return self.fee_bps
        fee = await self.fetch_fee_tier()
        self.fee_bps = fee["taker_fee_bps"]
        self.fee_source = "account_api"
        return self.fee_bps

    async def fetch_rate_limit_usage(self) -> dict:
        payload = await self._get(
            "/v1/rateLimit", params=self._account_params(),
            headers=self._account_headers())
        if not isinstance(payload, dict):
            raise RuntimeError("[ARCUS] rate-limit response is not an object")
        return payload

    async def warm_http(self) -> None:
        """No separate Arcus keepalive request is needed for account reads."""
        return None

    async def close(self) -> None:
        return None
