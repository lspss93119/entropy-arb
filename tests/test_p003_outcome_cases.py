"""P0-03 additional offline characterization tests. No network or real orders."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).parent))
from test_engine import make_engine
from entropy_arb.venue_lighter import LighterVenue, AccountOrdersFeed
from entropy_arb.config import load_config
from entropy_arb.venue_hl import HLVenue


def test_partial_terminal_fill_account_orders_is_preserved():
    async def check():
        feed=AccountOrdersFeed("RH","wss://example.invalid",32,1,None)
        fut=feed.watch(123)
        feed._handle_orders({"orders":{"32":[{"client_order_index":123,"status":"filled","filled_base_amount":"0.4","filled_quote_amount":"40"}]}})
        result=await fut
        assert result["filled_base"] == 0.4
        assert result["avg_px"] == 100.0
        # The feed treats a terminal partial fill as a resolved positive fill.
    asyncio.run(check())


def test_lighter_post_send_exception_is_incorrectly_marked_certain_zero(monkeypatch):
    async def check():
        cfg=load_config("/dev/null","/dev/null",symbol="SNDK",hedge_venue="lighter-rh") if False else make_engine().cfg
        venue=LighterVenue(cfg.hedge,None,0.01)
        venue.market_id=32
        venue.signer=SimpleNamespace()
        async def fail(**kwargs):
            raise TimeoutError("simulated response lost after exchange receives order")
        venue.signer.create_order=fail
        venue.orders_feed=None
        import sys
        monkeypatch.setitem(sys.modules,"lighter",SimpleNamespace(SignerClient=SimpleNamespace(
            ORDER_TYPE_MARKET=1,ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL=0,DEFAULT_IOC_EXPIRY=0)))
        res=await venue.send_taker(is_buy=True,qty=1,limit_px=100)
        assert res["status"]=="send-failed"
        assert res["unresolved"] is False
        assert res["filled_base"]==0
    asyncio.run(check())


def test_lighter_terminal_timeout_is_explicitly_unresolved(monkeypatch):
    async def check():
        cfg=make_engine().cfg
        venue=LighterVenue(cfg.hedge,None,0.002)
        venue.market_id=32
        async def send(**kwargs):
            return None,SimpleNamespace(code=200),None
        venue.signer=SimpleNamespace(create_order=send)
        venue.orders_feed=AccountOrdersFeed("RH","wss://example.invalid",32,1,None)
        monkeypatch.setitem(sys.modules,"lighter",SimpleNamespace(SignerClient=SimpleNamespace(
            ORDER_TYPE_MARKET=1,ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL=0,DEFAULT_IOC_EXPIRY=0)))
        res=await venue.send_taker(is_buy=True,qty=1,limit_px=100)
        assert res["status"]=="timeout"
        assert res["unresolved"] is True
        assert res["filled_base"]==0
        assert venue.orders_feed._pending=={}
    asyncio.run(check())


def test_hl_response_parses_partial_filled_size():
    res=HLVenue._parse({"status":"ok","response":{"data":{"statuses":[{"filled":{"totalSz":"0.4","avgPx":"101.2"}}]}}})
    assert res["status"]=="filled" and res["filled_base"]==0.4
    assert res["avg_px"]==101.2


def test_lighter_subsequent_late_terminal_not_consumed_by_timed_out_waiter():
    async def check():
        feed=AccountOrdersFeed("RH","wss://example.invalid",32,1,None)
        fut=feed.watch(77)
        feed.unwatch(77)
        feed._handle_orders({"orders":{"32":[{"client_order_index":77,"status":"filled","filled_base_amount":"1","filled_quote_amount":"100"}]}})
        assert fut.cancelled()
        assert 77 in feed._terminal
        # Late result is stored in transient memory but cannot update Engine's already-reported zero.
    asyncio.run(check())
