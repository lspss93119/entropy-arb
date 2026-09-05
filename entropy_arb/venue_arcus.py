"""Arcus market-data, account-read, and testnet execution adapter.

The adapter owns all Arcus-specific request construction and reconciliation.
The generic engine still refuses to run an Arcus pair live; the execution
methods here are deliberately usable only by the bounded single-venue
testnet ladder.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from decimal import (Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR)
from typing import Any, Mapping, Optional
from urllib.parse import quote

import aiohttp

from .book import OrderBook
from .config import ArcusCreds, VenueConf
from .feeds import ArcusBBOFeed

log = logging.getLogger("arcus")

REST_TIMEOUT = 10.0
GOOD_TIL_HORIZON_US = 32 * 24 * 60 * 60 * 1_000_000
ORDER_POLL_INTERVAL_SEC = 0.25


class _OrderValidationError(RuntimeError):
    """A local order request failed validation before any POST was sent."""


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


def _format_decimal(value: Decimal) -> str:
    """Format a positive Decimal without exponent notation or noise zeros."""
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


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
        self.tick_tiers: list[tuple[Decimal, Optional[Decimal]]] = []
        self.price_decimals = 0
        self.size_decimals = 0
        self.min_base = 0.0
        self.min_quote = 0.0
        self.max_order_size = 0.0
        self.min_base_decimal = Decimal("0")
        self.min_quote_decimal = Decimal("0")
        self.max_order_size_decimal = Decimal("0")
        self._client_seq = 0
        self._position_synced = False

    async def _get(self, path: str, params: Optional[dict] = None,
                   headers: Optional[Mapping[str, str]] = None):
        async with self.session.get(
                self.api_url + path, params=params,
                headers=dict(headers) if headers else None,
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as response:
            response.raise_for_status()
            return await response.json()

    async def _post(self, path: str, body: dict, *, params: Optional[dict],
                    headers: Mapping[str, str]):
        """POST one signed request and classify transport uncertainty.

        A 5xx or client timeout does not establish that the exchange rejected
        the request.  The caller must reconcile such an outcome instead of
        retrying or treating it as a zero fill.
        """
        try:
            async with self.session.post(
                    self.api_url + path, params=params, json=body,
                    headers=dict(headers),
                    timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as response:
                try:
                    payload = await response.json()
                except (TypeError, ValueError, aiohttp.ContentTypeError):
                    payload = None
                if response.status >= 500:
                    return response.status, payload, None, True
                if response.status >= 400:
                    return response.status, payload, (
                        f"HTTP {response.status}"), False
                if not isinstance(payload, dict):
                    return response.status, payload, "invalid Arcus response", True
                return response.status, payload, None, False
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
            return None, None, None, True

    def _execution_credentials(self) -> ArcusCreds:
        if self.environment != "testnet":
            raise RuntimeError("Arcus mainnet execution is not enabled")
        creds = self._credentials()
        if self.signer is None:
            self.init_signer()
        return creds

    @staticmethod
    def _result(status: str, *, filled_base: float = 0.0,
                avg_px: Optional[float] = None, err: Optional[str] = None,
                unresolved: bool = False) -> dict[str, Any]:
        return {"status": status, "filled_base": float(filled_base),
                "avg_px": avg_px, "err": err, "unresolved": unresolved}

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
        tick_tiers = []
        raw_tiers = market.get("tickTiers")
        if raw_tiers is not None:
            if not isinstance(raw_tiers, list) or not raw_tiers:
                raise RuntimeError("[ARCUS] tickTiers must be a non-empty list")
            for tier in raw_tiers:
                if not isinstance(tier, dict):
                    raise RuntimeError("[ARCUS] tickTiers row is not an object")
                tier_tick = _decimal(tier.get("tick"), "tickTiers.tick")
                upper = tier.get("upToPrice")
                upper_price = (None if upper is None else
                               _decimal(upper, "tickTiers.upToPrice"))
                tick_tiers.append((tier_tick, upper_price))
        if not tick_tiers:
            tick_tiers = [(tick_size, None)]

        self.market = market
        self.market_id = market_id
        self.market_display_name = display_name
        self.tick_size = tick_size
        self.step_size = step_size
        self.tick_tiers = tick_tiers
        self.price_decimals = _decimal_places(tick_size)
        self.size_decimals = _decimal_places(step_size)
        self.min_base = float(min_order_size)
        self.min_quote = float(min_order_notional)
        self.max_order_size = float(max_order_size)
        self.min_base_decimal = min_order_size
        self.min_quote_decimal = min_order_notional
        self.max_order_size_decimal = max_order_size
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

    def _next_client_id(self, timestamp_ns: int) -> str:
        self._client_seq += 1
        client_id = f"arcus-{timestamp_ns}-{self._client_seq}"
        if len(client_id) > 36:
            raise _OrderValidationError("Arcus clientId exceeds 36 characters")
        return client_id

    def _tick_for_price(self, price: Decimal) -> Decimal:
        tiers = self.tick_tiers or [(self.tick_size, None)]
        for tick, upper in tiers:
            if upper is None or price <= upper:
                return tick
        return tiers[-1][0]

    @staticmethod
    def _is_multiple(value: Decimal, step: Decimal) -> bool:
        units = value / step
        return units == units.to_integral_value()

    def _round_price(self, price: Decimal, *, is_buy: bool) -> Decimal:
        if price <= 0:
            raise _OrderValidationError("Arcus order price must be positive")
        rounding = ROUND_FLOOR if is_buy else ROUND_CEILING
        tick = self._tick_for_price(price)
        rounded = (price / tick).to_integral_value(rounding=rounding) * tick

        # A tier boundary can change the applicable tick.  Accept a rounded
        # boundary when it is valid under its own tier; otherwise re-round
        # once with that tier's tick while preserving the safety direction.
        for _ in range(3):
            if rounded > 0:
                applicable = self._tick_for_price(rounded)
                if self._is_multiple(rounded, applicable):
                    if ((is_buy and rounded <= price)
                            or (not is_buy and rounded >= price)):
                        return rounded
                tick = applicable
                rounded = (price / tick).to_integral_value(
                    rounding=rounding) * tick
            else:
                break
        raise _OrderValidationError("Arcus order price cannot be aligned safely")

    def _normalize_order(self, *, is_buy: bool, qty: Any, limit_px: Any,
                         time_in_force: str, reduce_only: bool) -> tuple[dict,
                                                                           dict,
                                                                           int,
                                                                           str]:
        if self.market_id < 0 or self.tick_size <= 0 or self.step_size <= 0:
            raise _OrderValidationError(
                "Arcus market metadata is required before placing an order")
        if not isinstance(time_in_force, str):
            raise _OrderValidationError("Arcus timeInForce is required")
        tif = time_in_force.upper()
        tif_code = {"IOC": 2, "ALO": 3}.get(tif)
        if tif_code is None:
            raise _OrderValidationError(
                "Arcus execution supports only IOC and ALO")

        try:
            requested_qty = _finite_decimal(qty, "order quantity")
            requested_px = _finite_decimal(limit_px, "order price")
        except RuntimeError as exc:
            raise _OrderValidationError(str(exc)) from exc
        if requested_qty <= 0:
            raise _OrderValidationError("Arcus order quantity must be positive")
        if requested_px <= 0:
            raise _OrderValidationError("Arcus order price must be positive")

        quantity = (requested_qty / self.step_size).to_integral_value(
            rounding=ROUND_FLOOR) * self.step_size
        if quantity < self.min_base_decimal:
            raise _OrderValidationError(
                "Arcus order quantity is below the market minimum")
        if quantity > self.max_order_size_decimal:
            raise _OrderValidationError(
                "Arcus order quantity exceeds the market maximum")

        price = self._round_price(requested_px, is_buy=is_buy)
        if price * quantity < self.min_quote_decimal:
            raise _OrderValidationError(
                "Arcus order notional is below the market minimum")

        quantity_units = quantity / self.step_size
        price_ticks = price / self.tick_size
        if (quantity_units != quantity_units.to_integral_value()
                or price_ticks != price_ticks.to_integral_value()):
            raise _OrderValidationError(
                "Arcus order does not align to the signed price/size grid")

        timestamp_ns = time.time_ns()
        if isinstance(timestamp_ns, bool) or timestamp_ns <= 0:
            raise _OrderValidationError("Arcus timestamp is invalid")
        good_til_us = timestamp_ns // 1000 + GOOD_TIL_HORIZON_US
        client_id = self._next_client_id(timestamp_ns)
        creds = self._credentials()
        address = _address(creds.account_address or "")
        account_index = int(creds.account_index)
        signed = {
            "ad": address,
            "ai": account_index,
            "c": client_id,
            "ct": timestamp_ns,
            "g": good_til_us * 1000,
            "m": self.market_id,
            "op": 1,
            "p": int(price_ticks),
            "q": int(quantity_units),
            "r": 1 if reduce_only else 0,
            "s": 0 if is_buy else 1,
            "t": tif_code,
            "v": 1,
        }
        body = {
            "address": address,
            "marketId": self.market_id,
            "accountIndex": account_index,
            "orderSide": "BUY" if is_buy else "SELL",
            "orderType": "LIMIT",
            "quantity": _format_decimal(quantity),
            "price": _format_decimal(price),
            "timeInForce": tif,
            "goodTilTime": str(good_til_us),
            "clientId": client_id,
            "timestamp": timestamp_ns,
            "reduceOnly": bool(reduce_only),
        }
        return body, signed, timestamp_ns, client_id

    @staticmethod
    def _order_id(payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        value = payload.get("orderId", payload.get("order_id"))
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _client_id(payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        value = payload.get("clientId", payload.get("client_id"))
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _filled_amount(payload: Mapping[str, Any]) -> Decimal:
        value = payload.get("filledSize", payload.get("filled_base"))
        if value is None and payload.get("originalSize") is not None:
            original = _finite_decimal(payload["originalSize"],
                                       "order original size")
            remaining = _finite_decimal(payload.get("remainingSize", 0),
                                        "order remaining size")
            value = original - remaining
        if value is None:
            return Decimal("0")
        filled = _finite_decimal(value, "order filled size")
        if filled < 0:
            raise RuntimeError("[ARCUS] order filled size cannot be negative")
        return filled

    @staticmethod
    def _average_price(payload: Mapping[str, Any], filled: Decimal) -> Optional[float]:
        value = payload.get("avgFillPrice", payload.get("avgPrice"))
        if value is None:
            value = payload.get("avg_px")
        if value is None:
            return None
        average = _finite_decimal(value, "order average fill price")
        if average <= 0 and filled > 0:
            raise RuntimeError("[ARCUS] order average fill price is invalid")
        return float(average) if average > 0 else None

    def _order_result(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return self._result("timeout", err="invalid Arcus order response",
                                unresolved=True)
        try:
            filled = self._filled_amount(payload)
            average = self._average_price(payload, filled)
        except RuntimeError:
            return self._result("timeout", err="invalid Arcus order response",
                                unresolved=True)

        status = str(payload.get("status", "")).upper()
        state = str(payload.get("state", "")).upper()
        rejection_reason = str(payload.get("rejectionReason", "")).upper()
        if rejection_reason in {"IOC_CANCELED", "FOK_FAILED"}:
            if filled > 0:
                return self._result("partially-filled",
                                    filled_base=float(filled), avg_px=average)
            return self._result("canceled")
        if status == "REJECTED" or state == "REJECTED":
            return self._result("rejected", err="Arcus order rejected")
        if status == "FILLED":
            return self._result("filled", filled_base=float(filled),
                                avg_px=average)
        if (status == "PARTIALLY_FILLED" or state == "PARTIALLY_FILLED"
                or (status in {"CANCELED", "IOC_CANCELED"} and filled > 0)):
            return self._result("partially-filled", filled_base=float(filled),
                                avg_px=average)
        if status in {"CANCELED", "IOC_CANCELED", "FOK_FAILED",
                      "MARGIN_CANCELED", "TPSL_CANCELED"}:
            return self._result("canceled", filled_base=float(filled),
                                avg_px=average)
        if status in {"OPEN", "PENDING", "ACK", "CANCEL_ACKNOWLEDGED",
                      "CANCEL_PENDING"}:
            return self._result("timeout", filled_base=float(filled),
                                avg_px=average, unresolved=True)
        return self._result("timeout", filled_base=float(filled),
                            avg_px=average,
                            err="unrecognized Arcus order status",
                            unresolved=True)

    def _ack_details(self, payload: Any, http_status: int,
                     generated_client_id: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {"status": "timeout", "filled_base": 0.0,
                    "avg_px": None, "err": "invalid Arcus response",
                    "unresolved": True, "client_id": generated_client_id}
        try:
            filled = self._filled_amount(payload)
            average = self._average_price(payload, filled)
        except RuntimeError:
            return {"status": "timeout", "filled_base": 0.0,
                    "avg_px": None, "err": "invalid Arcus response",
                    "unresolved": True, "client_id": generated_client_id}
        status = str(payload.get("status") or
                     ("ACK" if http_status == 202 else "ERROR")).upper()
        return {"status": status, "filled_base": float(filled),
                "avg_px": average, "err": None, "unresolved": False,
                "order_id": self._order_id(payload),
                "client_id": self._client_id(payload) or generated_client_id,
                "payload": payload}

    async def place_limit(self, *, is_buy: bool, qty: Any, limit_px: Any,
                          time_in_force: str,
                          reduce_only: bool = False) -> dict[str, Any]:
        """Submit one LIMIT order on the Arcus testnet.

        This low-level helper is intentionally single-order only.  It returns
        the gateway acknowledgement; ``send_taker`` owns terminal-state
        reconciliation for IOC orders.
        """
        self._execution_credentials()
        try:
            body, signed, timestamp_ns, client_id = self._normalize_order(
                is_buy=is_buy, qty=qty, limit_px=limit_px,
                time_in_force=time_in_force, reduce_only=reduce_only)
        except _OrderValidationError as exc:
            return {"status": "rejected", "filled_base": 0.0,
                    "avg_px": None, "err": str(exc), "unresolved": False}

        try:
            signature = self.signer.sign_scheme1(signed)
            headers = self.signer.headers(timestamp_ns, signature)
        except Exception:
            return {"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": "Arcus signing failed",
                    "unresolved": False, "client_id": client_id}

        http_status, payload, error, unresolved = await self._post(
            "/v1/placeOrder", body, params={"address": body["address"]},
            headers=headers)
        if error is not None or unresolved:
            return {"status": "timeout" if unresolved else "send-failed",
                    "filled_base": 0.0, "avg_px": None,
                    "err": error, "unresolved": unresolved,
                    "order_id": self._order_id(payload),
                    "client_id": self._client_id(payload) or client_id}
        return self._ack_details(payload, int(http_status or 0), client_id)

    async def send_taker(self, *, is_buy: bool, qty: Any, limit_px: Any,
                         reduce_only: bool = False) -> dict[str, Any]:
        """Submit an IOC limit order and reconcile every ambiguous outcome."""
        before_position = self.position if self._position_synced else None
        try:
            ack = await self.place_limit(
                is_buy=is_buy, qty=qty, limit_px=limit_px,
                time_in_force="IOC", reduce_only=reduce_only)
        except RuntimeError as exc:
            return self._result("send-failed", err=str(exc))

        if ack["status"] in {"rejected", "send-failed"}:
            return self._result(ack["status"],
                                filled_base=ack.get("filled_base", 0.0),
                                avg_px=ack.get("avg_px"),
                                err=ack.get("err"),
                                unresolved=ack.get("unresolved", False))

        payload = ack.get("payload")
        if payload is not None:
            direct = self._order_result(payload)
            if not direct["unresolved"]:
                self._apply_local_fill(direct, is_buy=is_buy)
                return direct

        try:
            requested_qty = _finite_decimal(qty, "order quantity")
        except RuntimeError:
            requested_qty = Decimal("0")
        return await self._reconcile_order(
            order_id=ack.get("order_id"), client_id=ack.get("client_id"),
            is_buy=is_buy, requested_qty=requested_qty,
            before_position=before_position)

    def _apply_local_fill(self, result: Mapping[str, Any], *, is_buy: bool) -> None:
        filled = float(result.get("filled_base", 0.0) or 0.0)
        if filled <= 0:
            return
        self.last_traded_ts = time.time()
        if self._position_synced:
            self.position += filled if is_buy else -filled

    async def _fetch_order_status(self, order_id: str) -> dict:
        path = "/v1/order/" + quote(str(order_id), safe="")
        payload = await self._get(path, params=self._account_params(),
                                  headers=self._account_headers())
        if not isinstance(payload, dict):
            raise RuntimeError("[ARCUS] order status response is not an object")
        return payload

    async def _fetch_order_history(self) -> list[dict]:
        payload = await self._get(
            "/v1/orders", params=self._account_params(),
            headers=self._account_headers())
        orders = payload.get("orders") if isinstance(payload, dict) else None
        if not isinstance(orders, list):
            raise RuntimeError("[ARCUS] order history response has no orders list")
        return orders

    async def _fetch_fills_for_order(self, order_id: Optional[str],
                                     client_id: Optional[str]) -> list[dict]:
        fills = await self.fetch_fills()
        matched = []
        for fill in fills:
            if not isinstance(fill, dict):
                continue
            same_order = (order_id is not None
                          and str(fill.get("orderId")) == str(order_id))
            same_client = (client_id is not None
                           and str(fill.get("clientId")) == str(client_id))
            if same_order or same_client:
                matched.append(fill)
        return matched

    def _fills_result(self, fills: list[dict],
                      requested_qty: Decimal) -> Optional[dict[str, Any]]:
        total = Decimal("0")
        notional = Decimal("0")
        try:
            for fill in fills:
                size = _finite_decimal(fill.get("size"), "fill size")
                price = _finite_decimal(fill.get("price"), "fill price")
                if size <= 0 or price <= 0:
                    raise RuntimeError("[ARCUS] fill size/price is invalid")
                total += size
                notional += size * price
        except (AttributeError, RuntimeError):
            return self._result("timeout", err="invalid Arcus fill response",
                                unresolved=True)
        if total <= 0:
            return None
        average = float(notional / total)
        status = ("filled" if requested_qty <= 0
                  or total >= requested_qty else "partially-filled")
        return self._result(status, filled_base=float(total), avg_px=average)

    async def _reconcile_order(self, *, order_id: Optional[str],
                               client_id: Optional[str], is_buy: bool,
                               requested_qty: Decimal,
                               before_position: Optional[float]) -> dict:
        deadline = time.monotonic() + max(0.0, self.settle_timeout)
        while True:
            if order_id:
                try:
                    snapshot = await self._fetch_order_status(order_id)
                    result = self._order_result(snapshot)
                    if not result["unresolved"]:
                        self._apply_local_fill(result, is_buy=is_buy)
                        return result
                except Exception:
                    pass

            try:
                fills = await self._fetch_fills_for_order(order_id, client_id)
                result = self._fills_result(fills, requested_qty)
                if result is not None:
                    if not result["unresolved"]:
                        self._apply_local_fill(result, is_buy=is_buy)
                    return result
            except Exception:
                pass

            if not order_id and client_id:
                try:
                    orders = await self._fetch_order_history()
                    for order in orders:
                        if (isinstance(order, dict)
                                and str(order.get("clientId")) == str(client_id)):
                            order_id = self._order_id(order)
                            if order_id:
                                break
                except Exception:
                    pass

            if before_position is not None:
                try:
                    current_position = await self.fetch_position()
                    delta = current_position - before_position
                    signed_delta = delta if is_buy else -delta
                    step = float(self.step_size)
                    if (signed_delta > 0 and requested_qty > 0
                            and signed_delta <= requested_qty + step):
                        status = ("filled" if signed_delta + step / 2
                                  >= float(requested_qty)
                                  else "partially-filled")
                        return self._result(status, filled_base=signed_delta)
                except Exception:
                    pass

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(ORDER_POLL_INTERVAL_SEC, remaining))

        return self._result("timeout", unresolved=True)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel exactly one server order by order ID on testnet."""
        self._execution_credentials()
        if not isinstance(order_id, str) or not order_id:
            return self._result("rejected", err="Arcus order ID is required")
        timestamp_ns = time.time_ns()
        creds = self._credentials()
        address = _address(creds.account_address or "")
        account_index = int(creds.account_index)
        signed = {"ad": address, "ai": account_index, "ct": timestamp_ns,
                  "id": order_id, "m": self.market_id, "op": 2, "v": 1}
        body = {"address": address, "marketId": self.market_id,
                "accountIndex": account_index, "kind": "orderId",
                "orderId": order_id, "timestamp": timestamp_ns}
        try:
            headers = self.signer.headers(
                timestamp_ns, self.signer.sign_scheme1(signed))
        except Exception:
            return self._result("send-failed", err="Arcus signing failed")

        http_status, payload, error, unresolved = await self._post(
            "/v1/cancelOrder", body, params={"address": address},
            headers=headers)
        if error is not None or unresolved:
            return self._result("timeout" if unresolved else "send-failed",
                                err=error, unresolved=unresolved)
        result = self._order_result(payload)
        if not result["unresolved"]:
            return result
        before_position = self.position if self._position_synced else None
        return await self._reconcile_order(
            order_id=order_id, client_id=None, is_buy=True,
            requested_qty=Decimal("0"), before_position=before_position)

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
        normalized = normalize_position(row) if row is not None else 0.0
        self.position = normalized
        self._position_synced = True
        return normalized

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
