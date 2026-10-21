"""Book freshness checks: transport activity versus exchange-event age."""
import asyncio
import time

from entropy_arb.book import OrderBook
from entropy_arb.feeds import HLBookFeed, LighterBookFeed, _source_time_seconds


def levels():
    return [[{"px": "99", "sz": "5"}], [{"px": "100", "sz": "5"}]]


def test_heartbeat_does_not_revive_stale_book():
    book = OrderBook()
    book.apply_hl(levels(), source_ts=time.time() - 80)
    book.touch()
    assert book.is_fresh(10)  # upstream compatibility when feature is disabled
    assert not book.is_fresh(10, 30)


def test_book_receive_time_is_checked_independently():
    book = OrderBook()
    book.apply_hl(levels(), source_ts=time.time())
    book.last_update_ts -= 80
    book.touch()
    assert not book.is_fresh(10, 30)


def test_invalid_missing_and_future_source_times_fail_closed():
    book = OrderBook()
    for stamp in (None, float("nan"), time.time() + 30):
        book.apply_hl(levels(), source_ts=stamp)
        assert not book.is_fresh(10, 30)
    book.apply_hl(levels(), source_ts=time.time())
    assert book.is_fresh(10, 30)


def test_reconnect_clear_resets_source_and_book_age():
    book = OrderBook()
    book.apply_hl(levels(), source_ts=time.time())
    assert book.is_fresh(10, 30)
    book.clear()
    book.touch()
    assert book.source_ts is None and not book.is_fresh(10, 30)


def test_hl_parses_event_time_and_ignores_other_coin_or_pong():
    book = OrderBook()
    feed = HLBookFeed("HL", "wss://example.invalid", "io:SNDK", book, lambda: None)
    old_time_ms = int((time.time() - 80) * 1000)
    feed._on_frame({"channel": "l2Book", "data":
                    {"coin": "io:SNDK", "levels": levels(), "time": old_time_ms}})
    feed._on_frame({"channel": "pong"})
    assert not book.is_fresh(10, 30)
    feed._on_frame({"channel": "l2Book", "data":
                    {"coin": "elsewhere", "levels": levels(), "time": int(time.time() * 1000)}})
    assert not book.is_fresh(10, 30)
    feed._on_frame({"channel": "l2Book", "data":
                    {"coin": "io:SNDK", "levels": levels(), "time": int(time.time() * 1000)}})
    assert book.is_fresh(10, 30)


def test_lighter_uses_engine_book_timestamp_instead_of_fresh_broadcast():
    async def run():
        book = OrderBook()
        feed = LighterBookFeed("RH", "wss://example.invalid", 2, book, lambda: None)

        class WS:
            async def send(self, _message):
                raise AssertionError("unexpected resubscribe")
        msg = {"channel": "order_book:2", "order_book": {
            "nonce": 10, "bids": [{"price": "99", "size": "5"}],
            "asks": [{"price": "100", "size": "5"}],
            "last_updated_at": int((time.time() - 80) * 1_000_000)},
            "timestamp": int(time.time() * 1000)}
        await feed._handle_book(WS(), msg, snapshot=True)
        assert not book.is_fresh(10, 30)
        diff = {"channel": "order_book:2", "order_book": {
            "begin_nonce": 11, "nonce": 11,
            "bids": [{"price": "99.1", "size": "1"}], "asks": [],
            "last_updated_at": int(time.time() * 1_000_000)}}
        await feed._handle_book(WS(), diff, snapshot=False)
        assert book.best_bid() == 99.1
        assert book.is_fresh(10, 30)
    asyncio.run(run())


def test_lighter_broadcast_fallback_and_missing_timestamp():
    async def run():
        book = OrderBook()
        feed = LighterBookFeed("Core", "wss://example.invalid", 2, book, lambda: None)

        class WS:
            async def send(self, _message):
                pass
        await feed._handle_book(WS(), {
            "channel": "order_book:2", "timestamp": int(time.time() * 1000),
            "order_book": {"nonce": 1, "bids": [{"price": "99", "size": "5"}],
                           "asks": [{"price": "100", "size": "5"}]}},
            snapshot=True)
        assert book.is_fresh(10, 30)
        await feed._handle_book(WS(), {
            "channel": "order_book:2",
            "order_book": {"nonce": 2, "begin_nonce": 2, "bids": [],
                           "asks": [{"price": "101", "size": "1"}]}},
            snapshot=False)
        assert not book.is_fresh(10, 30)
    asyncio.run(run())


def test_timestamp_parser_rejects_invalid_values_and_converts_units():
    for value in (None, "nan", "garbage", -1, True):
        assert _source_time_seconds(value, divisor=1000.0) is None
    assert _source_time_seconds(1_700_000_000_000, divisor=1000.0) == 1_700_000_000.0
    assert _source_time_seconds(1_700_000_000_000_000,
                                divisor=1_000_000.0) == 1_700_000_000.0
