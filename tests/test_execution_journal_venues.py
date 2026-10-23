"""Offline venue-result contracts for Execution Journal correlation."""
import asyncio
import sys
import types

import pytest

from entropy_arb.venue_hl import HLVenue
from entropy_arb.venue_lighter import AccountOrdersFeed, LighterVenue


def test_lighter_account_ws_keeps_exchange_order_id_and_stamps_trace():
    async def scenario():
        feed = AccountOrdersFeed("RH", "wss://fake", 32, 7, object())
        trace = {}
        feed._traces[123] = trace
        future = feed.watch(123)
        feed._handle_orders({"orders": {"32": [{
            "status": "filled",
            "client_order_index": 123,
            "order_id": 987654,
            "filled_base_amount": "1.25",
            "filled_quote_amount": "125.00",
        }]}})
        return await future, trace

    info, trace = asyncio.run(scenario())
    assert info["exchange_order_id"] == "987654"
    assert trace["exchange_order_id"] == "987654"
    assert "settlement_observed_mono_ns" in trace


def test_lighter_send_taker_returns_exchange_order_id(monkeypatch):
    class FakeSignerClient:
        ORDER_TYPE_MARKET = "market"
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = "ioc"
        DEFAULT_IOC_EXPIRY = 999

    monkeypatch.setitem(sys.modules, "lighter",
                        types.SimpleNamespace(SignerClient=FakeSignerClient))

    class FakeOrdersFeed:
        def __init__(self):
            self._traces = {}
            self._coi = None
            self._future = None

        def watch(self, coi):
            self._coi = coi
            self._future = asyncio.get_running_loop().create_future()
            return self._future

        def resolve(self):
            trace = self._traces.pop(self._coi, None)
            if trace is not None:
                trace["exchange_order_id"] = "987654"
                from entropy_arb.execution_journal import stamp
                stamp(trace, "settlement_observed")
            self._future.set_result({
                "status": "filled", "filled_base": 1.25,
                "filled_quote": 125.0, "avg_px": 100.0,
                "exchange_order_id": "987654",
            })

        def unwatch(self, coi, *, keep_trace=False):
            return None

    class FakeSigner:
        def __init__(self, feed):
            self.feed = feed

        async def create_order(self, **kwargs):
            asyncio.get_running_loop().call_soon(self.feed.resolve)
            return "tx", types.SimpleNamespace(code=200), None

    async def scenario():
        venue = LighterVenue.__new__(LighterVenue)
        venue.orders_feed = FakeOrdersFeed()
        venue.signer = FakeSigner(venue.orders_feed)
        venue._coi = 100
        venue.market_id = 32
        venue.price_decimals = 2
        venue.size_decimals = 4
        venue.settle_timeout = 1.0
        trace = {}
        result = await venue.send_taker(is_buy=True, qty=1.25,
                                         limit_px=100.0, trace=trace)
        return result, trace

    result, trace = asyncio.run(scenario())
    assert result["exchange_order_id"] == "987654"
    assert trace["exchange_order_id"] == "987654"
    assert trace["request_start_utc_ns"] <= trace["api_response_utc_ns"]
    assert trace["api_response_utc_ns"] <= trace["settlement_observed_utc_ns"]
    assert "settlement_observed_mono_ns" in trace


def test_lighter_send_taker_exception_is_unresolved(monkeypatch):
    class FakeSignerClient:
        ORDER_TYPE_MARKET = "market"
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = "ioc"
        DEFAULT_IOC_EXPIRY = 999

    monkeypatch.setitem(sys.modules, "lighter",
                        types.SimpleNamespace(SignerClient=FakeSignerClient))

    class FailingSigner:
        async def create_order(self, **kwargs):
            raise ConnectionError("websocket disconnected")

    class FakeOrdersFeed:
        def __init__(self):
            self._traces = {}

        def watch(self, coi):
            return asyncio.get_running_loop().create_future()

        def unwatch(self, coi, *, keep_trace=False):
            self._traces.pop(coi, None)

    async def scenario():
        venue = LighterVenue.__new__(LighterVenue)
        venue.signer = FailingSigner()
        venue.orders_feed = FakeOrdersFeed()
        venue._coi = 100
        venue.market_id = 32
        venue.price_decimals = 2
        venue.size_decimals = 4
        venue.settle_timeout = 1.0
        return await venue.send_taker(is_buy=True, qty=1.25,
                                      limit_px=100.0, trace={})

    result = asyncio.run(scenario())
    assert result["status"] == "unknown-exception"
    assert result["unresolved"] is True


def test_hyperliquid_ioc_parser_keeps_exchange_order_id():
    result = HLVenue._parse({
        "status": "ok",
        "response": {"data": {"statuses": [{
            "filled": {"totalSz": "1.25", "avgPx": "100.5", "oid": 24680}
        }]}}
    })
    assert result["exchange_order_id"] == "24680"
    assert result["filled_base"] == 1.25


def test_hyperliquid_ioc_timing_stamps_api_before_settlement():
    class FakeCloid:
        def to_raw(self):
            return 24680

    class FakeNonces:
        def next(self):
            return 123

    class FakeAccount:
        wallet = object()
        nonces = FakeNonces()
        is_mainnet = True

    class FakeSigning:
        @staticmethod
        def order_request_to_order_wire(request, asset_id):
            return request

        @staticmethod
        def order_wires_to_order_action(wires):
            return wires

        @staticmethod
        def sign_l1_action(*args):
            return "signature"

    async def scenario():
        venue = HLVenue.__new__(HLVenue)
        venue.account = FakeAccount()
        venue._signing = FakeSigning()
        venue.asset_id = 200000
        venue.coin = "io:SNDK"
        venue.settle_timeout = 1.0
        venue._next_cloid = lambda: FakeCloid()

        async def post_exchange(payload):
            return ({"status": "ok", "response": {"data": {"statuses": [{
                "filled": {"totalSz": "1", "avgPx": "100", "oid": 24680}
            }]}}}, None, False)

        venue._post_exchange = post_exchange
        trace = {}
        result = await venue.send_taker(is_buy=True, qty=1, limit_px=100,
                                        trace=trace)
        return result, trace

    result, trace = asyncio.run(scenario())
    assert result["exchange_order_id"] == "24680"
    assert trace["exchange_order_id"] == "24680"
    assert trace["api_response_utc_ns"] <= trace["settlement_observed_utc_ns"]


def test_hyperliquid_standard_equity_is_scoped_to_configured_dex():
    async def scenario():
        venue = HLVenue.__new__(HLVenue)
        venue.conf = types.SimpleNamespace(hl_dex="io")
        venue.include_core_equity = True
        venue.account = None
        venue._query_address = lambda: "0xuser"
        calls = []

        async def info(payload):
            calls.append(payload)
            if payload["type"] == "userAbstraction":
                return "disabled"
            return {"marginSummary": {"accountValue": "0"},
                    "withdrawable": "0"}

        venue._info = info
        return await venue.fetch_equity(), calls

    result, calls = asyncio.run(scenario())
    assert result == (0.0, 0.0)
    assert calls == [
        {"type": "userAbstraction", "user": "0xuser"},
        {"type": "clearinghouseState", "user": "0xuser", "dex": "io"},
    ]


def test_hyperliquid_unified_equity_uses_spot_collateral_state():
    async def scenario():
        venue = HLVenue.__new__(HLVenue)
        venue.conf = types.SimpleNamespace(hl_dex="io")
        venue.collateral_token = 0
        venue.include_core_equity = True
        venue.account = None
        venue._query_address = lambda: "0xuser"
        calls = []

        async def info(payload):
            calls.append(payload)
            if payload["type"] == "userAbstraction":
                return "unifiedAccount"
            if payload["type"] == "spotClearinghouseState":
                return {
                    "balances": [{"coin": "USDC", "token": 0,
                                  "total": "3679.140172", "hold": "0"}],
                    "tokenToAvailableAfterMaintenance": [[0, "3679.140172"]],
                }
            raise AssertionError(f"unexpected info request: {payload}")

        venue._info = info
        return await venue.fetch_equity(), calls

    result, calls = asyncio.run(scenario())
    assert result == (3679.140172, 3679.140172)
    assert calls == [
        {"type": "userAbstraction", "user": "0xuser"},
        {"type": "spotClearinghouseState", "user": "0xuser"},
    ]


def test_hyperliquid_unified_equity_can_be_excluded_for_shared_account():
    async def scenario():
        venue = HLVenue.__new__(HLVenue)
        venue.conf = types.SimpleNamespace(hl_dex="io")
        venue.collateral_token = 0
        venue.include_core_equity = False
        venue.account = None
        venue._query_address = lambda: "0xuser"

        async def info(payload):
            assert payload == {"type": "userAbstraction", "user": "0xuser"}
            return "unifiedAccount"

        venue._info = info
        return await venue.fetch_equity()

    assert asyncio.run(scenario()) == (0.0, 0.0)


def test_hyperliquid_equity_rejects_missing_authoritative_fields():
    async def scenario():
        venue = HLVenue.__new__(HLVenue)
        venue.conf = types.SimpleNamespace(hl_dex="io")
        venue.include_core_equity = True
        venue.account = None
        venue._query_address = lambda: "0xuser"

        async def info(payload):
            if payload["type"] == "userAbstraction":
                return "disabled"
            assert payload["type"] == "clearinghouseState"
            return {"marginSummary": {}}

        venue._info = info
        return await venue.fetch_equity()

    with pytest.raises(RuntimeError, match="accountValue"):
        asyncio.run(scenario())
