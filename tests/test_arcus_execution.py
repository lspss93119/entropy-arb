"""Offline Arcus testnet execution-contract tests.

These tests exercise request construction and bounded reconciliation without
opening a network connection.  The live ladder is intentionally kept out of
the unit-test suite.
"""
import asyncio
import json
import os
import sys
import tempfile
from decimal import Decimal
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import load_config  # noqa: E402


FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
PRIVATE_KEY = (
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
API_KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
ADDRESS = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
TIMESTAMP_NS = 1_700_000_000_000_000_000
GOOD_TIL_US = 1_702_764_800_000_000

MINIMAL = """
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
venues:
  arcus:
    taker_fee_bps: 2.25
    max_position_usd: 1000
    max_orders_per_min: 30
"""


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.headers = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self.payload

    async def text(self):
        return json.dumps(self.payload)


class FakeSession:
    def __init__(self, get_routes=None, post_routes=None):
        self.get_routes = get_routes or {}
        self.post_routes = post_routes or {}
        self.get_calls = []
        self.post_calls = []

    @staticmethod
    def _next(routes, path):
        value = routes[path]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"no fake response left for {path}")
            return value.pop(0)
        return value

    def get(self, url, params=None, headers=None, **kwargs):
        path = urlparse(url).path
        self.get_calls.append({"path": path, "params": params,
                               "headers": headers})
        value = self._next(self.get_routes, path)
        if isinstance(value, FakeResponse):
            return value
        payload, status = value if isinstance(value, tuple) else (value, 200)
        return FakeResponse(payload, status)

    def post(self, url, params=None, json=None, headers=None, **kwargs):
        path = urlparse(url).path
        self.post_calls.append({"path": path, "params": params,
                                "json": json, "headers": headers})
        value = self._next(self.post_routes, path)
        if isinstance(value, FakeResponse):
            return value
        payload, status = value if isinstance(value, tuple) else (value, 200)
        return FakeResponse(payload, status)


def make_venue(monkeypatch, *, get_routes=None, post_routes=None,
               settle_timeout=0.05, environment="testnet"):
    monkeypatch.setenv("ARCUS_ENV", environment)
    monkeypatch.setenv("ARCUS_API_KEY", API_KEY)
    monkeypatch.setenv("ARCUS_API_PRIVATE_KEY", PRIVATE_KEY)
    monkeypatch.setenv("ARCUS_ACCOUNT_ADDRESS", ADDRESS)
    monkeypatch.setenv("ARCUS_ACCOUNT_INDEX", "2")
    cfg = load_config(write_tmp(MINIMAL), NO_ENV, symbol="SNDK",
                      venue_a="arcus", venue_b="lighter-rh")
    from entropy_arb.venue_arcus import ArcusVenue
    session = FakeSession(get_routes or {
        "/v1/markets": fixture("arcus_markets_sndk.json")},
        post_routes=post_routes)
    venue = ArcusVenue(cfg.venue_a, cfg.arcus_api_url, cfg.arcus_ws_url,
                       session, settle_timeout, environment=environment)
    asyncio.run(venue.load_market())
    if environment == "testnet":
        venue.init_signer()
    return venue, session


def place_signed_payload(*, address=ADDRESS, account_index=2,
                         client_id="arcus-1700000000000000000-1",
                         timestamp=TIMESTAMP_NS, good_til=GOOD_TIL_US,
                         price_ticks=176292, quantity_quantums=100000,
                         side=0, tif=2, reduce_only=0):
    return {
        "ad": address,
        "ai": account_index,
        "c": client_id,
        "ct": timestamp,
        "g": good_til * 1000,
        "m": 33,
        "op": 1,
        "p": price_ticks,
        "q": quantity_quantums,
        "r": reduce_only,
        "s": side,
        "t": tif,
        "v": 1,
    }


def test_place_order_uses_signed_integer_payload_and_testnet_account_index(
        monkeypatch):
    module = __import__("entropy_arb.venue_arcus", fromlist=["ArcusVenue"])
    venue, session = make_venue(
        monkeypatch,
        post_routes={"/v1/placeOrder": ({
            "status": "ACK", "orderId": "order-1",
            "clientId": "arcus-1700000000000000000-1"}, 202)})
    monkeypatch.setattr(module.time, "time_ns", lambda: TIMESTAMP_NS)

    result = asyncio.run(venue.place_limit(
        is_buy=True, qty=Decimal("0.01000009"), limit_px=Decimal("1762.929"),
        time_in_force="IOC"))

    assert result["status"] == "ACK"
    request = session.post_calls[-1]
    assert request["path"] == "/v1/placeOrder"
    assert request["params"] == {"address": ADDRESS}
    assert request["json"] == {
        "address": ADDRESS,
        "marketId": 33,
        "accountIndex": 2,
        "orderSide": "BUY",
        "orderType": "LIMIT",
        "quantity": "0.01",
        "price": "1762.92",
        "timeInForce": "IOC",
        "goodTilTime": str(GOOD_TIL_US),
        "clientId": "arcus-1700000000000000000-1",
        "timestamp": TIMESTAMP_NS,
        "reduceOnly": False,
    }
    expected = place_signed_payload()
    assert request["headers"] == venue.signer.headers(
        TIMESTAMP_NS, venue.signer.sign_scheme1(expected))
    assert request["headers"]["X-Timestamp"] == str(TIMESTAMP_NS)
    assert expected["ai"] == request["json"]["accountIndex"] == 2
    assert expected["ct"] == TIMESTAMP_NS
    assert expected["g"] == GOOD_TIL_US * 1000


def test_price_rounding_is_directional_and_respects_tick_tiers(monkeypatch):
    venue, session = make_venue(
        monkeypatch,
        post_routes={"/v1/placeOrder": [
            ({"status": "ACK", "orderId": "buy-tier"}, 202),
            ({"status": "ACK", "orderId": "sell-tier"}, 202),
        ]})
    buy = asyncio.run(venue.place_limit(
        is_buy=True, qty=Decimal("0.01"), limit_px=Decimal("5000.019"),
        time_in_force="ALO"))
    sell = asyncio.run(venue.place_limit(
        is_buy=False, qty=Decimal("0.01"), limit_px=Decimal("5000.001"),
        time_in_force="ALO"))
    assert buy["status"] == sell["status"] == "ACK"
    assert session.post_calls[0]["json"]["price"] == "5000"
    assert session.post_calls[1]["json"]["price"] == "5000.02"
    assert Decimal(session.post_calls[0]["json"]["price"]) <= Decimal("5000.019")
    assert Decimal(session.post_calls[1]["json"]["price"]) >= Decimal("5000.001")


def test_invalid_or_undersized_order_is_rejected_without_post(monkeypatch):
    venue, session = make_venue(monkeypatch, post_routes={})
    result = asyncio.run(venue.send_taker(
        is_buy=True, qty=Decimal("0.0099999"), limit_px=Decimal("1762.92")))
    assert result["status"] == "rejected"
    assert result["filled_base"] == 0.0
    assert result["unresolved"] is False
    assert session.post_calls == []

    below_notional = asyncio.run(venue.send_taker(
        is_buy=True, qty=Decimal("0.01"), limit_px=Decimal("1")))
    assert below_notional["status"] == "rejected"
    assert session.post_calls == []

    above_max = asyncio.run(venue.send_taker(
        is_buy=True, qty=Decimal("100000.0000001"),
        limit_px=Decimal("1762.92")))
    assert above_max["status"] == "rejected"
    assert session.post_calls == []


def test_send_taker_distinguishes_rejected_zero_partial_and_full_outcomes(
        monkeypatch):
    order = {
        "orderId": "order-1", "status": "CANCELED",
        "state": "PARTIALLY_FILLED", "filledSize": "0.004",
        "remainingSize": "0.006", "avgFillPrice": "1762.91",
    }
    session_routes = {
        "/v1/order/order-1": order,
        "/v1/order/order-2": {
            "orderId": "order-2", "status": "CANCELED",
            "state": "CANCELED", "filledSize": "0",
            "remainingSize": "0.01",
        },
    }
    venue, session = make_venue(
        monkeypatch,
        get_routes={"/v1/markets": fixture("arcus_markets_sndk.json"),
                    **session_routes},
        post_routes={"/v1/placeOrder": [
            ({"status": "ACK", "orderId": "order-1"}, 202),
            ({"status": "ACK", "orderId": "order-2"}, 202),
            ({"status": "REJECTED", "orderId": "order-3",
              "rejectionReason": "UNDERCOLLATERALIZED"}, 200),
            ({"status": "FILLED", "orderId": "order-4",
              "filledSize": "0.01", "avgFillPrice": "1762.90"}, 200),
        ]})
    partial = asyncio.run(venue.send_taker(
        is_buy=True, qty=Decimal("0.01"), limit_px=Decimal("1762.92")))
    zero = asyncio.run(venue.send_taker(
        is_buy=True, qty=Decimal("0.01"), limit_px=Decimal("1762.92")))
    rejected = asyncio.run(venue.send_taker(
        is_buy=True, qty=Decimal("0.01"), limit_px=Decimal("1762.92")))
    filled = asyncio.run(venue.send_taker(
        is_buy=True, qty=Decimal("0.01"), limit_px=Decimal("1762.92")))
    assert partial == {"status": "partially-filled", "filled_base": 0.004,
                       "avg_px": 1762.91, "err": None, "unresolved": False}
    assert zero["status"] == "canceled"
    assert zero["filled_base"] == 0.0
    assert zero["unresolved"] is False
    assert rejected["status"] == "rejected"
    assert rejected["filled_base"] == 0.0
    assert filled == {"status": "filled", "filled_base": 0.01,
                      "avg_px": 1762.90, "err": None, "unresolved": False}


def test_send_taker_reconciles_from_fills_when_order_status_is_unavailable(
        monkeypatch):
    venue, session = make_venue(
        monkeypatch,
        get_routes={
            "/v1/markets": fixture("arcus_markets_sndk.json"),
            "/v1/order/order-fills": ({"error": "not found"}, 404),
            "/v1/fills": {"fills": [{
                "orderId": "order-fills", "clientId":
                "arcus-1700000000000000000-1", "size": "0.01",
                "price": "1762.88"}]},
        },
        post_routes={"/v1/placeOrder": ({
            "status": "ACK", "orderId": "order-fills"}, 202)},
        settle_timeout=0.01)
    result = asyncio.run(venue.send_taker(
        is_buy=True, qty=Decimal("0.01"), limit_px=Decimal("1762.92")))
    assert result == {"status": "filled", "filled_base": 0.01,
                      "avg_px": 1762.88, "err": None, "unresolved": False}
    assert any(call["path"] == "/v1/fills" for call in session.get_calls)


def test_ambiguous_timeout_is_unresolved_and_never_zero_fill(monkeypatch):
    venue, session = make_venue(
        monkeypatch,
        get_routes={
            "/v1/markets": fixture("arcus_markets_sndk.json"),
            "/v1/order/order-timeout": ({"error": "temporarily unavailable"},
                                         503),
            "/v1/fills": {"fills": []},
            "/v1/positions": {"positions": {}},
        },
        post_routes={"/v1/placeOrder": ({
            "status": "ACK", "orderId": "order-timeout"}, 202)},
        settle_timeout=0.01)
    result = asyncio.run(venue.send_taker(
        is_buy=True, qty=Decimal("0.01"), limit_px=Decimal("1762.92")))
    assert result["status"] == "timeout"
    assert result["filled_base"] == 0.0
    assert result["unresolved"] is True
    assert result["err"] is None


def test_cancel_order_signs_order_id_and_uses_single_cancel_endpoint(
        monkeypatch):
    module = __import__("entropy_arb.venue_arcus", fromlist=["ArcusVenue"])
    venue, session = make_venue(
        monkeypatch,
        post_routes={"/v1/cancelOrder": ({
            "status": "CANCELED", "orderId": "order-1"}, 200)})
    monkeypatch.setattr(module.time, "time_ns", lambda: TIMESTAMP_NS)
    result = asyncio.run(venue.cancel_order("order-1"))
    assert result["status"] == "canceled"
    request = session.post_calls[-1]
    assert request["path"] == "/v1/cancelOrder"
    assert request["params"] == {"address": ADDRESS}
    assert request["json"] == {
        "address": ADDRESS,
        "marketId": 33,
        "accountIndex": 2,
        "kind": "orderId",
        "orderId": "order-1",
        "timestamp": TIMESTAMP_NS,
    }
    expected = {"ad": ADDRESS, "ai": 2, "ct": TIMESTAMP_NS,
                "id": "order-1", "m": 33, "op": 2, "v": 1}
    assert request["headers"] == venue.signer.headers(
        TIMESTAMP_NS, venue.signer.sign_scheme1(expected))


def test_reduce_only_is_carried_in_body_and_ordersign_payload(monkeypatch):
    module = __import__("entropy_arb.venue_arcus", fromlist=["ArcusVenue"])
    venue, session = make_venue(
        monkeypatch,
        post_routes={"/v1/placeOrder": ({
            "status": "ACK", "orderId": "reduce-only"}, 202)})
    monkeypatch.setattr(module.time, "time_ns", lambda: TIMESTAMP_NS)
    result = asyncio.run(venue.place_limit(
        is_buy=False, qty=Decimal("0.01"), limit_px=Decimal("1763.10"),
        time_in_force="IOC", reduce_only=True))
    assert result["status"] == "ACK"
    request = session.post_calls[-1]
    assert request["json"]["reduceOnly"] is True
    assert request["json"]["timeInForce"] == "IOC"
    assert request["headers"] == venue.signer.headers(
        TIMESTAMP_NS, venue.signer.sign_scheme1({
            "ad": ADDRESS, "ai": 2,
            "c": "arcus-1700000000000000000-1", "ct": TIMESTAMP_NS,
            "g": GOOD_TIL_US * 1000, "m": 33, "op": 1, "p": 176310,
            "q": 100000, "r": 1, "s": 1, "t": 2, "v": 1,
        }))


def test_mainnet_execution_is_hard_blocked_before_any_post(monkeypatch):
    venue, session = make_venue(monkeypatch, post_routes={}, environment="mainnet")
    result = asyncio.run(venue.send_taker(
        is_buy=True, qty=Decimal("0.01"), limit_px=Decimal("1762.92")))
    assert result["status"] == "send-failed"
    assert result["err"] == "Arcus mainnet execution is not enabled"
    assert result["unresolved"] is False
    assert session.post_calls == []


def test_rejection_error_does_not_echo_private_key(monkeypatch):
    venue, session = make_venue(
        monkeypatch,
        post_routes={"/v1/placeOrder": ({
            "error": "invalid request", "debug": PRIVATE_KEY}, 400)})
    result = asyncio.run(venue.send_taker(
        is_buy=True, qty=Decimal("0.01"), limit_px=Decimal("1762.92")))
    assert PRIVATE_KEY not in (result["err"] or "")
    assert result["status"] == "send-failed"
    assert session.post_calls
