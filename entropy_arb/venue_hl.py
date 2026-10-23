"""Hyperliquid HIP-3 dex venue adapter (Entropy = dex "io", trade.xyz = "xyz").

Market metadata, account state and order posting use Hyperliquid's public
/info and /exchange REST endpoints via plain aiohttp; the book comes from the
OFFICIAL websocket (see feeds.HLBookFeed). Trading lazily imports the
official `hyperliquid-python-sdk` signing helpers + eth_account —
--record-only data collection needs neither.

IOC limit orders settle synchronously in the /exchange response; unknown
outcomes (timeout/5xx) fall back to orderStatus-by-cloid polling inside
send_taker(), so the engine sees the same unified result shape as the Lighter
venue: {status, filled_base, avg_px, err, unresolved}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Optional

import aiohttp

from .book import OrderBook
from .config import VenueConf
from .feeds import HLBookFeed

log = logging.getLogger("hl")

INFO_TIMEOUT = 10.0


class NonceAllocator:
    def __init__(self) -> None:
        self._last = 0

    def next(self) -> int:
        self._last = max(self._last + 1, int(time.time() * 1000))
        return self._last


class HLAccount:
    def __init__(self, private_key: str, account_address: Optional[str],
                 api_url: str) -> None:
        from eth_account import Account
        self.wallet = Account.from_key(private_key)
        self.query_address = (account_address or self.wallet.address).lower()
        self.is_mainnet = api_url == "https://api.hyperliquid.xyz"
        self.nonces = NonceAllocator()

    def describe(self) -> str:
        s = f"signer={self.wallet.address} account={self.query_address}"
        if self.wallet.address.lower() != self.query_address:
            s += " (agent mode)"
        return s


class HLVenue:
    kind = "hl"

    def __init__(self, conf: VenueConf, api_url: str, ws_url: str,
                 session: aiohttp.ClientSession, settle_timeout_sec: float) -> None:
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.api_url = api_url
        self.ws_url = ws_url
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.volume_usd = 0.0     # cumulative filled notional this session
        self.equity = None
        self.free = None
        self.start_equity = None
        self.include_core_equity = True  # cleared when two venues share one account
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.account: Optional[HLAccount] = None
        self.coin = ""
        self.asset_id = -1
        self.collateral_token: Optional[int] = None
        self.size_decimals = 0
        self.min_base = 0.0
        self.min_quote = 10.0
        self._cloid = int(time.time() * 1000)
        self._signing = None      # lazy hyperliquid-sdk signing module

    async def _info(self, payload: dict):
        async with self.session.post(
                self.api_url + "/info", json=payload,
                timeout=aiohttp.ClientTimeout(total=INFO_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    async def load_market(self) -> None:
        dexs = await self._info({"type": "perpDexs"})
        names = [(d or {}).get("name", "") for d in dexs]
        if self.conf.hl_dex not in names:
            raise RuntimeError(f"[{self.name}] dex '{self.conf.hl_dex}' not "
                               f"found on Hyperliquid (available: "
                               f"{[n for n in names if n][:20]}...)")
        dex_index = names.index(self.conf.hl_dex)
        meta = await self._info({"type": "meta", "dex": self.conf.hl_dex})
        self.collateral_token = meta.get("collateralToken")
        want = f"{self.conf.hl_dex}:{self.conf.symbol}"
        for idx, a in enumerate(meta["universe"]):
            if a["name"] not in (want, self.conf.symbol):
                continue
            if a.get("isDelisted"):
                raise RuntimeError(f"[{self.name}] {a['name']} is delisted")
            self.coin = a["name"]
            self.asset_id = 110000 + (dex_index - 1) * 10000 + idx
            self.size_decimals = int(a["szDecimals"])
            self.min_base = 10 ** -self.size_decimals
            log.info("[%s] %s asset_id=%d szDecimals=%d maxLev=%sx %s",
                     self.name, self.coin, self.asset_id, self.size_decimals,
                     a.get("maxLeverage"),
                     "isolated-only" if a.get("onlyIsolated") else "")
            return
        raise RuntimeError(f"[{self.name}] {want} not found")

    def init_signer(self) -> None:
        c = self.conf.hl_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        try:
            from hyperliquid.utils import signing as hl_signing
        except ImportError as e:
            raise RuntimeError(
                "live trading on Hyperliquid needs the official SDK — "
                "pip install -r requirements-live.txt "
                "(hyperliquid-python-sdk)") from e
        self._signing = hl_signing
        self.account = HLAccount(c.private_key, c.account_address, self.api_url)
        log.info("[%s] %s", self.name, self.account.describe())

    def share_nonces_with(self, other: "HLVenue") -> None:
        """One signer address must use one nonce sequence."""
        if (self.account and other.account and
                self.account.wallet.address == other.account.wallet.address):
            other.account.nonces = self.account.nonces
            log.info("[%s]/[%s] same signer — shared nonce allocator",
                     self.name, other.name)

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        return [asyncio.create_task(
            HLBookFeed(self.name, self.ws_url, self.coin, self.book,
                       notify).run(stop),
            name=f"book-{self.key}")]

    def ready_to_trade(self) -> bool:
        return self.account is not None

    async def warm_http(self) -> None:
        """Order-path keepalive ping (driven by the engine's keepalive loop)."""
        try:
            await self._info({"type": "exchangeStatus"})
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        if px <= 0:
            return px
        max_dec = max(0, 6 - self.size_decimals)
        sig_dec = 4 - math.floor(math.log10(px))
        dec = max(0, min(max_dec, sig_dec))
        f = 10.0 ** dec
        v = math.ceil(px * f - 1e-9) / f if round_up else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    # ------------------------------------------------------------- execution

    def _next_cloid(self):
        from hyperliquid.utils.types import Cloid
        self._cloid += 1
        return Cloid.from_int(self._cloid)

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False, trace: Optional[dict] = None) -> dict:
        assert self.account is not None and self.asset_id >= 0
        s = self._signing
        cloid = self._next_cloid()
        if trace is not None:
            from .execution_journal import stamp
            trace['cloid'] = str(cloid.to_raw())
            stamp(trace, 'submit_enter')
        order_req = {"coin": self.coin, "is_buy": is_buy, "sz": round(qty, 8),
                     "limit_px": limit_px,
                     "order_type": {"limit": {"tif": "Ioc"}},
                     "reduce_only": reduce_only, "cloid": cloid}
        try:
            wire = s.order_request_to_order_wire(order_req, self.asset_id)
            action = s.order_wires_to_order_action([wire])
            nonce = self.account.nonces.next()
            sig = s.sign_l1_action(self.account.wallet, action, None, nonce,
                                   None, self.account.is_mainnet)
            payload = {"action": action, "nonce": nonce, "signature": sig,
                       "vaultAddress": None, "expiresAfter": None}
        except Exception as e:
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": f"signing failed: {e!r}", "unresolved": False}

        if trace is not None:
            stamp(trace, 'request_start')
        body, err, unresolved = await self._post_exchange(payload)
        if trace is not None:
            stamp(trace, 'api_response')
        if err is not None:
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": err, "unresolved": False}
        if not unresolved:
            res = self._parse(body)
            if not res.get("unresolved"):
                if trace is not None:
                    if res.get("exchange_order_id") is not None:
                        trace["exchange_order_id"] = res["exchange_order_id"]
                    stamp(trace, 'settlement_observed')
                return res
        # unknown outcome: poll orderStatus by cloid until the deadline
        deadline = time.time() + self.settle_timeout
        while time.time() < deadline:
            try:
                st = await self._info({"type": "orderStatus",
                                       "user": self.account.query_address,
                                       "oid": cloid.to_raw()})
            except Exception:
                st = None
            if st and st.get("status") == "order":
                o = st.get("order") or {}
                status = str(o.get("status", ""))
                inner = o.get("order") or {}
                try:
                    filled = max(float(inner.get("origSz") or 0)
                                 - float(inner.get("sz") or 0), 0.0)
                except (TypeError, ValueError):
                    filled = 0.0
                if status != "open":
                    exchange_order_id = o.get("oid") or inner.get("oid")
                    if trace is not None:
                        if exchange_order_id is not None:
                            trace["exchange_order_id"] = str(exchange_order_id)
                        stamp(trace, "settlement_observed")
                    return {"status": status, "filled_base": filled,
                            "avg_px": None, "err": None, "unresolved": False,
                            "exchange_order_id": (
                                str(exchange_order_id)
                                if exchange_order_id is not None else None)}
            await asyncio.sleep(0.5)
        return {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                "err": None, "unresolved": True}

    async def _post_exchange(self, payload: dict):
        try:
            async with self.session.post(
                    self.api_url + "/exchange", json=payload,
                    timeout=aiohttp.ClientTimeout(total=INFO_TIMEOUT)) as r:
                text = await r.text()
                if r.status == 429:
                    return None, f"RATE_LIMITED: HTTP 429 {text[:150]}", False
                if 400 <= r.status < 500:
                    return None, f"HTTP {r.status}: {text[:250]}", False
                if r.status >= 500:
                    return None, None, True
                return json.loads(text), None, False
        except (asyncio.TimeoutError, aiohttp.ClientError, json.JSONDecodeError):
            return None, None, True

    @staticmethod
    def _parse(body: dict) -> dict:
        def fail(msg: str) -> dict:
            low = msg.lower()
            if "rate limit" in low or "too many" in low:
                msg = "RATE_LIMITED: " + msg
            return {"status": "send-failed", "filled_base": 0.0, "avg_px": None,
                    "err": msg, "unresolved": False}
        if body.get("status") == "err":
            return fail(str(body.get("response")))
        if body.get("status") != "ok":
            return fail(f"unexpected response: {str(body)[:200]}")
        try:
            st = body["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError):
            return fail(f"malformed response: {str(body)[:200]}")
        if "filled" in st:
            f = st["filled"]
            exchange_order_id = (f.get("oid") or st.get("oid")
                                 or f.get("order_id"))
            return {"status": "filled",
                    "filled_base": float(f.get("totalSz") or 0.0),
                    "avg_px": float(f["avgPx"]) if f.get("avgPx") else None,
                    "err": None, "unresolved": False,
                    "exchange_order_id": (
                        str(exchange_order_id)
                        if exchange_order_id is not None else None)}
        if "error" in st:
            msg = str(st["error"])
            if "could not immediately match" in msg.lower():
                return {"status": "canceled", "filled_base": 0.0, "avg_px": None,
                        "err": None, "unresolved": False}
            return fail(msg)
        if "resting" in st:
            exchange_order_id = st["resting"].get("oid") if isinstance(
                st["resting"], dict) else None
            return {"status": "resting?", "filled_base": 0.0, "avg_px": None,
                    "err": None, "unresolved": True,
                    "exchange_order_id": (
                        str(exchange_order_id)
                        if exchange_order_id is not None else None)}
        return fail(f"unknown status: {str(st)[:150]}")

    # -------------------------------------------------------------- accounts

    def _query_address(self):
        if self.account is not None:
            return self.account.query_address
        c = self.conf.hl_creds
        return c.account_address.lower() if c and c.account_address else None

    async def fetch_equity(self):
        """Return equity and available margin for this venue's account scope.

        Unified and portfolio-margin accounts expose their cross-venue
        collateral through ``spotClearinghouseState``.  Standard accounts
        use the configured DEX's ``clearinghouseState``.  Missing or
        non-finite authoritative fields are errors rather than zero-valued
        balances.
        """
        addr = self._query_address()
        if addr is None:
            return None
        abstraction = await self._info({"type": "userAbstraction",
                                        "user": addr})
        if not isinstance(abstraction, str):
            raise RuntimeError("userAbstraction returned a non-string")
        if abstraction in ("unifiedAccount", "portfolioMargin"):
            if not self.include_core_equity:
                return 0.0, 0.0
            if self.collateral_token is None:
                raise RuntimeError(
                    f"missing collateral token for dex {self.conf.hl_dex!r}")
            state = await self._info({"type": "spotClearinghouseState",
                                      "user": addr})
            if not isinstance(state, dict):
                raise RuntimeError(
                    "spotClearinghouseState returned a non-object")
            balances = state.get("balances")
            if not isinstance(balances, list):
                raise RuntimeError(
                    "spotClearinghouseState missing balances")
            balance = next((b for b in balances
                            if isinstance(b, dict)
                            and b.get("token") == self.collateral_token), None)
            if not isinstance(balance, dict) or "total" not in balance:
                raise RuntimeError(
                    f"spotClearinghouseState missing collateral token "
                    f"{self.collateral_token}")
            available_map = state.get("tokenToAvailableAfterMaintenance")
            available = None
            if isinstance(available_map, dict):
                available = available_map.get(str(self.collateral_token))
                if available is None:
                    available = available_map.get(self.collateral_token)
            elif isinstance(available_map, list):
                for item in available_map:
                    if (isinstance(item, (list, tuple)) and len(item) == 2
                            and item[0] == self.collateral_token):
                        available = item[1]
                        break
            if available is None:
                raise RuntimeError(
                    "spotClearinghouseState missing available-after-"
                    "maintenance collateral value")
            try:
                eq = float(balance["total"])
                free = float(available)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "spotClearinghouseState has non-numeric collateral "
                    "fields") from exc
            if not math.isfinite(eq) or not math.isfinite(free):
                raise RuntimeError(
                    "spotClearinghouseState has non-finite collateral "
                    "fields")
            return eq, free

        state = await self._info({"type": "clearinghouseState", "user": addr,
                                  "dex": self.conf.hl_dex})
        if not isinstance(state, dict):
            raise RuntimeError("clearinghouseState returned a non-object")
        ms = state.get("marginSummary")
        if not isinstance(ms, dict) or "accountValue" not in ms:
            raise RuntimeError(
                f"clearinghouseState missing accountValue for dex "
                f"{self.conf.hl_dex!r}")
        if "withdrawable" not in state:
            raise RuntimeError(
                f"clearinghouseState missing withdrawable for dex "
                f"{self.conf.hl_dex!r}")
        try:
            eq = float(ms["accountValue"])
            free = float(state["withdrawable"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"clearinghouseState has non-numeric equity fields for dex "
                f"{self.conf.hl_dex!r}") from exc
        if not math.isfinite(eq) or not math.isfinite(free):
            raise RuntimeError(
                f"clearinghouseState has non-finite equity fields for dex "
                f"{self.conf.hl_dex!r}")
        return eq, free

    async def fetch_position(self) -> float:
        addr = self._query_address()
        assert addr is not None
        st = await self._info({"type": "clearinghouseState", "user": addr,
                               "dex": self.conf.hl_dex})
        for ap in st.get("assetPositions") or []:
            pos = ap.get("position") or {}
            if pos.get("coin") == self.coin:
                return float(pos.get("szi") or 0.0)
        return 0.0

    async def close(self) -> None:
        pass
