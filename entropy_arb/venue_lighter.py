"""zkLighter venue adapter (Lighter mainnet / Lighter Robinhood chain).

Market data and account state come from Lighter's public REST + websocket
APIs via plain aiohttp/websockets, so --record-only data collection works
without the SDK. Trading lazily imports the official `lighter` SDK
(https://github.com/elliottech/lighter-python) for transaction signing only.

Market orders carry mandatory avg-execution-price protection and settle
asynchronously on the authenticated account_orders websocket; send_taker()
hides that behind the same result shape the HL venue returns:
{status, filled_base, avg_px, err, reason, unresolved}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import aiohttp

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook
from .config import VenueConf
from .feeds import LighterBookFeed

log = logging.getLogger("lighter")

OPEN_STATUSES = {"in-progress", "pending", "open"}
AUTH_REFRESH_SEC = 8 * 60
REST_TIMEOUT = 10.0


@dataclass
class PreparedNonce:
    """An SDK nonce reserved for one pair commit.

    The application lock stays held while this object is live.  A token is
    either consumed by the RH transport or explicitly aborted; it cannot be
    returned to the nonce pool or used by a later order.
    """

    venue: "LighterVenue"
    api_key_index: int
    nonce: int
    nonce_wait_started_ts: str
    nonce_wait_completed_ts: str
    nonce_wait_ms: float
    nonce_prepared_ts: str
    nonce_prepare_ms: float
    consumed: bool = False
    aborted: bool = False
    lock_held: bool = True


class AccountOrdersFeed:
    """Authenticated stream of our own order updates (settlement channel)."""

    def __init__(self, name: str, ws_url: str, market_id: int,
                 account_index: int, signer) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.account_index = account_index
        self.signer = signer
        self.ready = asyncio.Event()
        self._pending: dict[int, asyncio.Future] = {}
        self._terminal: OrderedDict[int, dict] = OrderedDict()

    def watch(self, coi: int) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if coi in self._terminal:
            fut.set_result(self._terminal[coi])
            return fut
        self._pending[coi] = fut
        return fut

    def unwatch(self, coi: int) -> None:
        fut = self._pending.pop(coi, None)
        if fut is not None and not fut.done():
            fut.cancel()

    def _resolve(self, coi: int, info: dict) -> None:
        self._terminal[coi] = info
        while len(self._terminal) > 512:
            self._terminal.popitem(last=False)
        fut = self._pending.pop(coi, None)
        if fut is not None and not fut.done():
            fut.set_result(info)

    def _handle_orders(self, msg: dict) -> None:
        for lst in (msg.get("orders") or {}).values():
            for o in lst or []:
                status = str(o.get("status", ""))
                if status in OPEN_STATUSES:
                    continue
                try:
                    coi = int(o.get("client_order_index"))
                except (TypeError, ValueError):
                    continue
                fb = float(o.get("filled_base_amount") or 0.0)
                fq = float(o.get("filled_quote_amount") or 0.0)
                self._resolve(coi, {
                    "status": status, "filled_base": fb,
                    "filled_quote": fq,
                    "avg_px": (fq / fb) if fb > 0 else None,
                    "reason": "" if status.lower() == "filled" else status,
                })

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                auth, err = self.signer.create_auth_token_with_expiry()
                if err is not None:
                    raise RuntimeError(f"auth token: {err}")
                connected_at = time.time()
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t in ("subscribed/account_orders", "update/account_orders"):
                            if t.startswith("subscribed"):
                                log.info("[%s] account orders stream ready", self.name)
                            self.ready.set()
                            self._handle_orders(msg)
                        elif t == "connected":
                            await ws.send(json.dumps({
                                "type": "subscribe",
                                "channel": f"account_orders/{self.market_id}/"
                                           f"{self.account_index}",
                                "auth": auth}))
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        if stop.is_set():
                            break
                        if (time.time() - connected_at > AUTH_REFRESH_SEC
                                and not self._pending):
                            log.info("[%s] refreshing account ws auth", self.name)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] account ws error: %s — retry in %.0fs",
                            self.name, e, backoff)
                self.ready.clear()
                if stop.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self.ready.clear()


class LighterVenue:
    kind = "lighter"

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        assert conf.lighter_profile is not None
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.profile = conf.lighter_profile
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0     # cumulative filled notional this session
        self.equity = None
        self.free = None
        self.start_equity = None
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.market_id = -1
        self.price_decimals = 2
        self.size_decimals = 4
        self.min_base = 0.0
        self.min_quote = 10.0
        self.signer = None
        self.orders_feed: Optional[AccountOrdersFeed] = None
        self._coi = int(time.time() * 1000)
        self._nonce_lock = asyncio.Lock()

    # ------------------------------------------------------------------ REST

    async def _get(self, path: str, params: Optional[dict] = None):
        async with self.session.get(
                self.profile.api_url + path, params=params,
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    # ------------------------------------------------------------- lifecycle

    async def load_market(self) -> None:
        data = await self._get("/api/v1/orderBooks")
        for ob in data.get("order_books") or []:
            if ob.get("symbol") != self.conf.symbol:
                continue
            if ob.get("status") != "active":
                raise RuntimeError(f"[{self.name}] market status={ob.get('status')}")
            self.market_id = int(ob["market_id"])
            self.price_decimals = int(ob["supported_price_decimals"])
            self.size_decimals = int(ob["supported_size_decimals"])
            self.min_base = float(ob["min_base_amount"])
            self.min_quote = float(ob["min_quote_amount"])
            log.info("[%s] %s market_id=%d px_dec=%d sz_dec=%d min_base=%s "
                     "min_quote=%s taker_fee=%s", self.name, ob["symbol"],
                     self.market_id, self.price_decimals, self.size_decimals,
                     ob["min_base_amount"], ob["min_quote_amount"],
                     ob.get("taker_fee"))
            return
        raise RuntimeError(f"[{self.name}] {self.conf.symbol} not found on "
                           f"{self.profile.name}")

    def init_signer(self) -> None:
        c = self.conf.lighter_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        try:
            from lighter import SignerClient, nonce_manager
        except ImportError as e:
            raise RuntimeError(
                "live trading on Lighter needs the official SDK — "
                "pip install -r requirements-live.txt "
                "(git+https://github.com/elliottech/lighter-python.git)") from e
        signer = SignerClient(
            url=self.profile.api_url,
            account_index=c.account_index,
            api_private_keys={c.api_key_index: c.api_private_key},
            chain_id=self.profile.chain_id,
            nonce_management_type=nonce_manager.NonceManagerType.API,
        )
        err = signer.check_client()
        if err is not None:
            raise RuntimeError(f"[{self.name}] API key check failed: {err}")
        self.signer = signer
        log.info("[%s] signer ready (account %d)", self.name, c.account_index)

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        tasks = [asyncio.create_task(
            LighterBookFeed(self.name, self.profile.ws_url, self.market_id,
                            self.book, notify).run(stop),
            name=f"book-{self.key}")]
        if live:
            self.orders_feed = AccountOrdersFeed(
                self.name, self.profile.ws_url, self.market_id,
                self.conf.lighter_creds.account_index, self.signer)
            tasks.append(asyncio.create_task(self.orders_feed.run(stop),
                                             name=f"acct-{self.key}"))
        return tasks

    def ready_to_trade(self) -> bool:
        return self.orders_feed is not None and self.orders_feed.ready.is_set()

    async def warm_http(self) -> None:
        """Keep the order-path HTTPS connections warm (a cold TLS handshake
        adds 10-15ms to the first order after an idle spell)."""
        try:
            await self._get("/api/v1/status")
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)
        if self.signer is None:
            return
        try:
            sess = self.signer.api_client.rest_client.pool_manager
            async with sess.get(self.profile.api_url + "/api/v1/status",
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
                await r.read()
        except Exception as e:
            log.debug("[%s] signer keepalive failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        f = 10 ** self.price_decimals
        v = math.ceil(px * f - 1e-9) / f if round_up else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    # ------------------------------------------------------------- execution

    def _next_coi(self) -> int:
        self._coi += 1
        return self._coi

    @staticmethod
    def _utc_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")

    async def prepare_nonce(self) -> PreparedNonce:
        """Reserve one authoritative SDK nonce before a pair-level commit."""
        assert self.signer is not None
        creds = self.conf.lighter_creds
        assert creds is not None and creds.api_key_index is not None
        await self._nonce_lock.acquire()
        prepare_started = time.perf_counter()
        nonce_wait_started_ts = self._utc_timestamp()
        nonce_started = time.perf_counter()
        try:
            try:
                api_key_index, nonce = (
                    await self.signer.nonce_manager.async_next_nonce(
                        creds.api_key_index))
            finally:
                nonce_wait_ms = max(
                    0.0, (time.perf_counter() - nonce_started) * 1000.0)
                nonce_wait_completed_ts = self._utc_timestamp()
            nonce_prepared_ts = self._utc_timestamp()
            return PreparedNonce(
                venue=self,
                api_key_index=api_key_index,
                nonce=nonce,
                nonce_wait_started_ts=nonce_wait_started_ts,
                nonce_wait_completed_ts=nonce_wait_completed_ts,
                nonce_wait_ms=nonce_wait_ms,
                nonce_prepared_ts=nonce_prepared_ts,
                nonce_prepare_ms=max(
                    0.0, (time.perf_counter() - prepare_started) * 1000.0),
            )
        except BaseException:
            self._nonce_lock.release()
            raise

    def release_prepared_nonce(self, prepared: PreparedNonce) -> None:
        """Abort a prepared nonce; it is never returned for reuse."""
        if not isinstance(prepared, PreparedNonce) or prepared.venue is not self:
            raise RuntimeError("prepared nonce belongs to another venue")
        if prepared.consumed:
            raise RuntimeError("prepared nonce already consumed")
        if prepared.aborted or not prepared.lock_held:
            raise RuntimeError("prepared nonce already released")
        prepared.aborted = True
        prepared.lock_held = False
        self._nonce_lock.release()

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False,
                         submit_guard: Optional[Callable[[], bool]] = None,
                         prepared_nonce: Optional[PreparedNonce] = None) -> dict:
        """Market order with avg-price protection; settle via account ws."""
        assert self.signer is not None
        from lighter import SignerClient
        nonce_wait_started_ts = None
        nonce_wait_completed_ts = None
        nonce_wait_ms = None
        venue_guard_ts = None
        transport_attempted = False
        nonce_prepared_ts = (prepared_nonce.nonce_prepared_ts
                             if prepared_nonce is not None else None)
        nonce_prepare_ms = (prepared_nonce.nonce_prepare_ms
                            if prepared_nonce is not None else None)

        def with_diagnostics(result):
            result = dict(result)
            result.update({
                "nonce_wait_started_ts": nonce_wait_started_ts,
                "nonce_wait_completed_ts": nonce_wait_completed_ts,
                "nonce_wait_ms": nonce_wait_ms,
                "nonce_prepared_ts": nonce_prepared_ts,
                "nonce_prepare_ms": nonce_prepare_ms,
                "venue_guard_ts": venue_guard_ts,
                "transport_attempted": transport_attempted,
            })
            return result

        coi = self._next_coi()
        fut = self.orders_feed.watch(coi) if self.orders_feed else None
        base_amount = int(round(qty * 10 ** self.size_decimals))
        price = int(round(limit_px * 10 ** self.price_decimals))
        try:
            creds = self.conf.lighter_creds
            assert creds is not None and creds.api_key_index is not None
            prepared_lock_held = False
            if prepared_nonce is not None:
                if (not isinstance(prepared_nonce, PreparedNonce)
                        or prepared_nonce.venue is not self
                        or prepared_nonce.consumed
                        or prepared_nonce.aborted
                        or not prepared_nonce.lock_held):
                    raise RuntimeError("invalid or already released prepared nonce")
                api_key_index, nonce = (prepared_nonce.api_key_index,
                                        prepared_nonce.nonce)
                nonce_wait_started_ts = prepared_nonce.nonce_wait_started_ts
                nonce_wait_completed_ts = prepared_nonce.nonce_wait_completed_ts
                nonce_wait_ms = prepared_nonce.nonce_wait_ms
                prepared_lock_held = True
            else:
                # Refresh from the server and hold one application-level lock
                # through submission.  This is deliberately not a retry: an
                # invalid nonce remains a failed execution and keeps the
                # existing fail-closed behavior.
                await self._nonce_lock.acquire()
                try:
                    nonce_wait_started_ts = self._utc_timestamp()
                    nonce_started = time.perf_counter()
                    try:
                        api_key_index, nonce = (
                            await self.signer.nonce_manager.async_next_nonce(
                                creds.api_key_index))
                    finally:
                        nonce_wait_ms = max(
                            0.0, (time.perf_counter() - nonce_started) * 1000.0)
                        nonce_wait_completed_ts = self._utc_timestamp()
                    if submit_guard is not None:
                        venue_guard_ts = self._utc_timestamp()
                        allowed = submit_guard()
                        guard_result = getattr(submit_guard, "last_result", {}) or {}
                    else:
                        allowed = True
                        guard_result = {}
                    if not allowed:
                        if fut is not None:
                            self.orders_feed.unwatch(coi)
                        reason = (guard_result.get("reason") or "unknown")
                        return with_diagnostics({
                            "status": "pre-submit-blocked", "filled_base": 0.0,
                            "avg_px": None, "err": None, "unresolved": False,
                            "reason": f"range_guard:{reason}",
                            "not_submitted": True,
                        })
                    log.debug("[%s] submit coi=%d api_key=%d nonce=%d "
                              "reduce_only=%s", self.name, coi, api_key_index,
                              nonce, reduce_only)
                    transport_attempted = True
                    _tx, resp, err = await self.signer.create_order(
                        market_index=self.market_id,
                        client_order_index=coi,
                        base_amount=base_amount,
                        price=price,
                        is_ask=not is_buy,
                        order_type=SignerClient.ORDER_TYPE_MARKET,
                        time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                        reduce_only=reduce_only,
                        order_expiry=SignerClient.DEFAULT_IOC_EXPIRY,
                        nonce=nonce,
                        api_key_index=api_key_index,
                    )
                finally:
                    self._nonce_lock.release()
            if prepared_lock_held:
                try:
                    if submit_guard is not None:
                        venue_guard_ts = self._utc_timestamp()
                        allowed = submit_guard()
                        guard_result = getattr(submit_guard, "last_result", {}) or {}
                    else:
                        allowed = True
                        guard_result = {}
                    if not allowed:
                        if fut is not None:
                            self.orders_feed.unwatch(coi)
                        prepared_nonce.aborted = True
                        reason = (guard_result.get("reason") or "unknown")
                        return with_diagnostics({
                            "status": "pre-submit-blocked", "filled_base": 0.0,
                            "avg_px": None, "err": None, "unresolved": False,
                            "reason": f"range_guard:{reason}",
                            "not_submitted": True,
                        })
                    log.debug("[%s] submit coi=%d api_key=%d nonce=%d "
                              "reduce_only=%s", self.name, coi, api_key_index,
                              nonce, reduce_only)
                    transport_attempted = True
                    _tx, resp, err = await self.signer.create_order(
                        market_index=self.market_id,
                        client_order_index=coi,
                        base_amount=base_amount,
                        price=price,
                        is_ask=not is_buy,
                        order_type=SignerClient.ORDER_TYPE_MARKET,
                        time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                        reduce_only=reduce_only,
                        order_expiry=SignerClient.DEFAULT_IOC_EXPIRY,
                        nonce=nonce,
                        api_key_index=api_key_index,
                    )
                finally:
                    prepared_nonce.consumed = not prepared_nonce.aborted
                    prepared_nonce.lock_held = False
                    self._nonce_lock.release()
        except Exception as e:
            if fut is not None:
                self.orders_feed.unwatch(coi)
            msg = f"{type(e).__name__}: {e}"
            if getattr(e, "status", None) == 429 or "(429)" in str(e):
                msg = "RATE_LIMITED: " + msg
            return with_diagnostics({"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": msg, "reason": msg,
                    "unresolved": False})
        if err is not None or (getattr(resp, "code", 200) or 200) != 200:
            if fut is not None:
                self.orders_feed.unwatch(coi)
            msg = str(err) if err is not None else \
                f"tx rejected code={resp.code} msg={getattr(resp, 'message', None)}"
            if "rate limit" in msg.lower():
                msg = "RATE_LIMITED: " + msg
            return with_diagnostics({"status": "send-failed", "filled_base": 0.0,
                    "avg_px": None, "err": msg, "reason": msg,
                    "unresolved": False})
        if fut is None:
            return with_diagnostics({"status": "sent-unconfirmed", "filled_base": 0.0,
                    "avg_px": None, "err": None,
                    "reason": "no_account_orders_feed", "unresolved": True})
        try:
            info = await asyncio.wait_for(fut, timeout=self.settle_timeout)
            return with_diagnostics({"status": info["status"], "filled_base": info["filled_base"],
                    "avg_px": info.get("avg_px"), "err": None,
                    "reason": info.get("reason") or (
                        "" if info["status"].lower() == "filled"
                        else info["status"]),
                    "unresolved": False})
        except asyncio.TimeoutError:
            self.orders_feed.unwatch(coi)
            log.warning("[%s] no settle confirmation for coi %d in %.1fs",
                        self.name, coi, self.settle_timeout)
            return with_diagnostics({"status": "timeout", "filled_base": 0.0,
                    "avg_px": None, "err": None,
                    "reason": "settle_timeout", "unresolved": True})

    # -------------------------------------------------------------- accounts

    async def _account(self) -> Optional[dict]:
        c = self.conf.lighter_creds
        if c is None or c.account_index is None:
            return None
        data = await self._get("/api/v1/account",
                               params={"by": "index",
                                       "value": str(c.account_index)})
        for acct in data.get("accounts") or []:
            return acct
        return None

    async def fetch_equity(self):
        acct = await self._account()
        if acct is None:
            return None
        return (float(acct.get("total_asset_value") or 0.0),
                float(acct.get("available_balance") or 0.0))

    async def fetch_position(self) -> float:
        acct = await self._account()
        if acct is None:
            raise RuntimeError(f"[{self.name}] account not found")
        for p in acct.get("positions") or []:
            if int(p.get("market_id", -1)) == self.market_id:
                return float(p.get("sign") or 1.0) * float(p.get("position") or 0.0)
        return 0.0

    async def close(self) -> None:
        if self.signer is not None:
            try:
                await self.signer.api_client.close()
            except Exception:
                pass
