"""Offline Lighter unknown-outcome and late-settlement contracts."""
import asyncio
import json
import sys
import types

from entropy_arb.execution_journal import ExecutionJournal
from entropy_arb.venue_lighter import AccountOrdersFeed, LighterVenue


def _install_lighter_sdk(monkeypatch):
    class FakeSignerClient:
        ORDER_TYPE_MARKET = "market"
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = "ioc"
        DEFAULT_IOC_EXPIRY = 999

    monkeypatch.setitem(sys.modules, "lighter",
                        types.SimpleNamespace(SignerClient=FakeSignerClient))


def _venue(journal, feed, signer):
    venue = LighterVenue.__new__(LighterVenue)
    venue.key = "hedge"
    venue.name = "Lighter RH"
    venue.journal = journal
    venue.orders_feed = feed
    venue.signer = signer
    venue._coi = 100
    venue.market_id = 32
    venue.price_decimals = 2
    venue.size_decimals = 4
    venue.settle_timeout = 1.0
    return venue


def test_transport_exception_late_fill_is_journaled_once(tmp_path, monkeypatch):
    _install_lighter_sdk(monkeypatch)

    async def scenario():
        journal = ExecutionJournal(str(tmp_path / "late-transport.jsonl"),
                                   enabled=True)
        journal.start()
        message = {"orders": {"32": [{
            "status": "filled", "client_order_index": 101,
            "order_id": 987654, "filled_base_amount": "1.0",
            "filled_quote_amount": "100.0",
        }]}}

        class FailingSigner:
            calls = 0

            async def create_order(self, **kwargs):
                self.calls += 1
                asyncio.get_running_loop().call_soon(feed._handle_orders,
                                                     message)
                raise ConnectionError("transport lost after acceptance")

        feed = AccountOrdersFeed("RH", "wss://fake", 32, 7, object())
        signer = FailingSigner()
        venue = _venue(journal, feed, signer)
        feed.on_late_settlement = venue._on_late_settlement
        result = await venue.send_taker(
            is_buy=True, qty=1.0, limit_px=100.0,
            trace={"execution_id": "exec-transport"})
        await asyncio.sleep(0)
        # A duplicate terminal frame must not be counted or journaled twice.
        feed._handle_orders(message)
        await asyncio.sleep(0)
        await journal.close()
        events = [json.loads(line) for line in
                  (tmp_path / "late-transport.jsonl").read_text().splitlines()]
        return result, signer.calls, feed._terminal[101], events

    result, calls, terminal, events = asyncio.run(scenario())
    assert result["status"] == "unknown-exception"
    assert result["unresolved"] is True
    assert result["filled_base"] == 0.0
    assert calls == 1
    assert terminal["exchange_order_id"] == "987654"
    late = [event for event in events
            if event["event_type"] == "late_settlement"]
    assert len(late) == 1
    assert late[0]["execution_id"] == "exec-transport"
    assert late[0]["exchange_order_id"] == "987654"
    assert late[0]["filled_base"] == 1.0


def test_timeout_late_partial_fill_is_journaled_once(tmp_path, monkeypatch):
    _install_lighter_sdk(monkeypatch)

    async def scenario():
        journal = ExecutionJournal(str(tmp_path / "late-timeout.jsonl"),
                                   enabled=True)
        journal.start()

        class AcceptedSigner:
            calls = 0

            async def create_order(self, **kwargs):
                self.calls += 1
                return "tx", types.SimpleNamespace(code=200), None

        feed = AccountOrdersFeed("RH", "wss://fake", 32, 7, object())
        signer = AcceptedSigner()
        venue = _venue(journal, feed, signer)
        venue.settle_timeout = 0.001
        feed.on_late_settlement = venue._on_late_settlement
        result = await venue.send_taker(
            is_buy=False, qty=1.0, limit_px=100.0,
            trace={"execution_id": "exec-timeout"})
        message = {"orders": {"32": [{
            "status": "filled", "client_order_index": 101,
            "order_id": 24680, "filled_base_amount": "0.4",
            "filled_quote_amount": "39.8",
        }]}}
        feed._handle_orders(message)
        await asyncio.sleep(0)
        feed._handle_orders(message)
        await asyncio.sleep(0)
        await journal.close()
        events = [json.loads(line) for line in
                  (tmp_path / "late-timeout.jsonl").read_text().splitlines()]
        return result, signer.calls, events

    result, calls, events = asyncio.run(scenario())
    assert result["status"] == "timeout"
    assert result["unresolved"] is True
    assert result["filled_base"] == 0.0
    assert calls == 1
    late = [event for event in events
            if event["event_type"] == "late_settlement"]
    assert len(late) == 1
    assert late[0]["execution_id"] == "exec-timeout"
    assert late[0]["exchange_order_id"] == "24680"
    assert late[0]["filled_base"] == 0.4


def test_settlement_before_transport_exception_is_not_marked_unknown(
        monkeypatch):
    _install_lighter_sdk(monkeypatch)

    async def scenario():
        message = {"orders": {"32": [{
            "status": "filled", "client_order_index": 101,
            "order_id": 13579, "filled_base_amount": "0.7",
            "filled_quote_amount": "70.0",
        }]}}

        class FailingAfterSettlementSigner:
            async def create_order(self, **kwargs):
                feed._handle_orders(message)
                raise ConnectionError("response lost after settlement")

        feed = AccountOrdersFeed("RH", "wss://fake", 32, 7, object())
        signer = FailingAfterSettlementSigner()
        venue = _venue(None, feed, signer)
        result = await venue.send_taker(
            is_buy=True, qty=0.7, limit_px=100.0,
            trace={"execution_id": "exec-race", "leg_id": "hedge"})
        return result

    result = asyncio.run(scenario())
    assert result["status"] == "filled"
    assert result["unresolved"] is False
    assert result["filled_base"] == 0.7
    assert result["exchange_order_id"] == "13579"


def test_late_hedge_settlement_rearms_reconcile_and_keeps_leg_id(tmp_path):
    async def scenario():
        journal = ExecutionJournal(str(tmp_path / "late-hedge.jsonl"),
                                   enabled=True)
        journal.start()
        feed = AccountOrdersFeed("RH", "wss://fake", 32, 7, object())
        venue = _venue(journal, feed, object())
        reconcile_ids = []
        venue.on_reconcile_needed = reconcile_ids.append
        venue._on_late_settlement(
            101,
            {"status": "filled", "filled_base": 0.25,
             "avg_px": 100.0, "exchange_order_id": "24680"},
            {"execution_id": "exec-hedge", "leg_id": "hedge:hedge"})
        await journal.close()
        events = [json.loads(line) for line in
                  (tmp_path / "late-hedge.jsonl").read_text().splitlines()]
        return events, reconcile_ids

    events, reconcile_ids = asyncio.run(scenario())
    assert reconcile_ids == ["exec-hedge"]
    assert len(events) == 1
    assert events[0]["event_type"] == "late_settlement"
    assert events[0]["leg_id"] == "hedge:hedge"
