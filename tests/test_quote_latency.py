"""Read-only quote timing and executable-edge research contracts."""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

from entropy_arb.book import OrderBook
from entropy_arb.feeds import HLBookFeed, LighterBookFeed
from entropy_arb.quote_latency import (
    BoundedJsonlWriter,
    EdgeDecayTracker,
    FeedQuote,
    executable_edges,
    extract_source_timestamp,
    normalize_feed_update,
)


def _hl_message(time_value=1_700_000_123_456):
    return {
        "channel": "l2Book",
        "data": {
            "coin": "io:SNDK",
            "time": time_value,
            "levels": [
                [{"px": "100.0", "sz": "4"},
                 {"px": "99.9", "sz": "5"}],
                [{"px": "100.1", "sz": "3"},
                 {"px": "100.2", "sz": "6"}],
            ],
        },
    }


def _rh_message():
    return {
        "type": "subscribed/order_book",
        "channel": "order_book/32",
        "order_book": {
            "nonce": 10,
            "bids": [{"price": "100.0", "size": "4"},
                     {"price": "99.9", "size": "5"}],
            "asks": [{"price": "100.1", "size": "3"},
                     {"price": "100.2", "size": "6"}],
        },
    }


def test_hl_source_time_is_milliseconds_and_age_is_not_zero():
    info = extract_source_timestamp(_hl_message(), "entropy")

    assert info.status == "present"
    assert info.field == "data.time"
    assert info.unit == "milliseconds"
    assert info.source_timestamp_ns == 1_700_000_123_456_000_000
    assert info.age_ms(1_700_000_123_999_000_000) == 543.0


def test_integer_source_milliseconds_preserve_nanosecond_conversion():
    info = extract_source_timestamp(
        {"channel": "order_book:32", "timestamp": 1_792_812_890_621},
        "lighter-rh",
    )

    assert info.source_timestamp_ns == 1_792_812_890_621_000_000


def test_rh_missing_source_time_remains_unknown_not_zero_latency():
    info = extract_source_timestamp(_rh_message(), "lighter-rh")

    assert info.status == "missing"
    assert info.source_timestamp_ns is None
    assert info.age_ms(1_700_000_123_456_000_000) is None


def test_invalid_source_time_is_distinguished_from_missing():
    info = extract_source_timestamp(
        {"channel": "order_book/32", "order_book": {"timestamp": "bad"}},
        "lighter-rh",
    )

    assert info.status == "invalid"
    assert info.source_timestamp_ns is None
    assert info.field == "order_book.timestamp"


def test_normalize_feed_update_keeps_bounded_sorted_depth():
    book = OrderBook()
    book.apply_hl(_hl_message()["data"]["levels"])

    quote = normalize_feed_update(
        "entropy", "snapshot", _hl_message(),
        received_wall_ns=1_700_000_124_000_000_000,
        received_mono_ns=123,
        connection_id=7,
        book=book,
        depth_levels=1,
    )

    assert quote.bids == ((100.0, 4.0),)
    assert quote.asks == ((100.1, 3.0),)
    assert quote.received_wall_ns == 1_700_000_124_000_000_000
    assert quote.connection_id == 7
    assert quote.source_timestamp.status == "present"


def test_hl_callback_receives_snapshot_and_local_clocks():
    book = OrderBook()
    updates = []
    feed = HLBookFeed(
        "entropy", "wss://example.invalid", "io:SNDK", book,
        lambda: None,
        on_update=lambda *args: updates.append(args),
    )
    feed._connection_id = 3
    feed._on_frame(_hl_message(), 10_000, 20_000)

    assert len(updates) == 1
    venue, kind, raw, wall_ns, mono_ns, connection_id = updates[0]
    assert (venue, kind, raw["channel"], wall_ns, mono_ns, connection_id) == (
        "entropy", "snapshot", "l2Book", 10_000, 20_000, 3)


def test_lighter_callback_receives_snapshot_and_diff():
    book = OrderBook()
    updates = []
    feed = LighterBookFeed(
        "lighter-rh", "wss://example.invalid", 32, book,
        lambda: None,
        on_update=lambda *args: updates.append(args),
    )
    feed._connection_id = 9

    class FakeWS:
        sent = []

        async def send(self, value):
            self.sent.append(value)

    async def exercise():
        await feed._handle_book(FakeWS(), _rh_message(), snapshot=True,
                                received_wall_ns=30, received_mono_ns=40)
        await feed._handle_book(
            FakeWS(),
            {"channel": "order_book/32", "order_book": {
                "begin_nonce": 11, "nonce": 11,
                "bids": [{"price": "100.05", "size": "2"}],
                "asks": [],
            }},
            snapshot=False, received_wall_ns=50, received_mono_ns=60,
        )

    asyncio.run(exercise())
    assert [item[1] for item in updates] == ["snapshot", "diff"]
    assert updates[0][0] == "lighter-rh"
    assert updates[1][3:6] == (50, 60, 9)


def test_lighter_nonce_gap_emits_lifecycle_and_drops_book():
    book = OrderBook()
    updates = []
    lifecycle = []
    feed = LighterBookFeed(
        "lighter-rh", "wss://example.invalid", 32, book,
        lambda: None,
        on_update=lambda *args: updates.append(args),
        on_lifecycle=lambda *args: lifecycle.append(args),
    )
    feed._connection_id = 4

    class FakeWS:
        sent = []

        async def send(self, value):
            self.sent.append(value)

    async def exercise():
        ws = FakeWS()
        await feed._handle_book(ws, _rh_message(), snapshot=True)
        await feed._handle_book(
            ws,
            {"channel": "order_book:32", "order_book": {
                "begin_nonce": 20, "nonce": 20,
                "bids": [], "asks": [],
            }},
            snapshot=False,
        )
        return ws

    ws = asyncio.run(exercise())
    assert [item[1] for item in updates] == ["snapshot"]
    assert lifecycle[0][1] == "book_gap"
    assert not book.ready
    assert any("unsubscribe" in item for item in ws.sent)


def test_lighter_feed_reconnects_with_new_connection_id(monkeypatch):
    import entropy_arb.feeds as feeds

    stop = asyncio.Event()
    lifecycle = []
    connections = []

    class FakeWS:
        def __init__(self, index):
            self.index = index
            self.sent = []
            self.failed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.failed:
                self.failed = True
                return json.dumps({"type": "connected"})
            if self.index == 0:
                raise RuntimeError("simulated disconnect")
            raise StopAsyncIteration

        async def send(self, value):
            self.sent.append(value)

    def fake_connect(*args, **kwargs):
        ws = FakeWS(len(connections))
        connections.append(ws)
        return ws

    real_sleep = asyncio.sleep

    async def fast_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr(feeds, "ws_connect", fake_connect)
    monkeypatch.setattr(feeds.asyncio, "sleep", fast_sleep)

    def on_lifecycle(*args):
        lifecycle.append(args)
        if args[1] == "connected" and args[4] == 2:
            stop.set()

    feed = LighterBookFeed(
        "lighter-rh", "wss://example.invalid", 32, OrderBook(),
        lambda: None, on_lifecycle=on_lifecycle,
    )
    asyncio.run(feed.run(stop))

    assert [item[1] for item in lifecycle] == [
        "connected", "disconnected", "connected", "disconnected"
    ]
    assert [item[4] for item in lifecycle] == [1, 1, 2, 2]


def _quote(venue, wall_ns, bid, ask):
    return FeedQuote(
        venue=venue, kind="diff", received_wall_ns=wall_ns,
        received_mono_ns=wall_ns, connection_id=1,
        source_timestamp=extract_source_timestamp({}, venue),
        bids=((bid, 5.0),), asks=((ask, 5.0),),
    )


def _params(valid=True):
    return SimpleNamespace(
        valid=valid, effective_at=1_700_000_000,
        window_start=1_699_971_200, window_end=1_700_000_000,
        sample_count=480, coverage=1.0,
        midline_bps=0.0, upper_bps=2.0, lower_bps=3.0,
        invalid_reason=None if valid else "data_gap",
    )


def test_executable_edges_match_recorder_definitions():
    entropy = _quote("entropy", 1_000_000_000, 101.0, 102.0)
    hedge = _quote("lighter-rh", 1_001_000_000, 100.0, 100.5)

    result = executable_edges(entropy, hedge)

    assert result.sell_edge_bps == (101.0 / 100.5 - 1.0) * 1e4
    assert result.buy_edge_bps == (100.0 / 102.0 - 1.0) * 1e4
    assert result.pair_skew_ms == 1.0
    assert result.eligible is True


def test_edge_tracker_records_crossing_and_all_decay_horizons():
    base = 1_700_000_000_000_000_000
    tracker = EdgeDecayTracker(pair_max_skew_ms=250)
    params = _params()

    records = tracker.ingest(_quote("entropy", base, 101.0, 101.0), params)
    records += tracker.ingest(
        _quote("lighter-rh", base + 1_000_000, 100.0, 100.5), params)
    assert [r["record_type"] for r in records] == ["signal"]
    signal_id = records[0]["signal_id"]
    assert records[0]["direction"] == "sell_entropy"
    assert records[0]["threshold_bps"] == 2.0

    all_records = []
    for offset_ms in (60, 110, 260, 510, 1010):
        records = tracker.ingest(
            _quote("entropy", base + offset_ms * 1_000_000,
                   101.0, 101.0), params)
        all_records += records
        if offset_ms == 1010:
            all_records += tracker.flush(base + 1011 * 1_000_000)

    decay = [r for r in all_records if r["record_type"] == "decay"]
    # The first four observations are emitted by later updates; the final
    # horizon is emitted by flush after the last quote update.
    assert {r["target_ms"] for r in decay} == {50, 100, 250, 500, 1000}
    assert all(r["signal_id"] == signal_id for r in decay)
    assert all(r["status"] == "observed" for r in decay)


def test_invalid_parameters_do_not_create_candidate_signal():
    base = 1_700_000_000_000_000_000
    tracker = EdgeDecayTracker()

    tracker.ingest(_quote("entropy", base, 101.0, 101.0), _params(False))
    records = tracker.ingest(
        _quote("lighter-rh", base + 1_000_000, 100.0, 100.5),
        _params(False),
    )

    assert not any(r["record_type"] == "signal" for r in records)


def test_bounded_writer_rotates_and_reports_rows(tmp_path):
    writer = BoundedJsonlWriter(
        tmp_path, "quotes", max_rows=2, max_bytes=10_000,
        retention_hours=24, max_files=10, run_id="test",
    )
    for value in range(5):
        writer.append({"value": value})
    writer.close()

    files = sorted(tmp_path.glob("quotes-test-*.jsonl"))
    assert len(files) == 3
    assert sum(path.read_text().count("\n") for path in files) == 5
    assert writer.stats["rows_written"] == 5
    assert writer.stats["rotations"] == 2


def test_bounded_writer_expires_old_files_within_its_prefix(tmp_path):
    old = tmp_path / "quotes-old-0001.jsonl"
    old.write_text('{"old":true}\n')
    old_time = time.time() - 7_200
    os.utime(old, (old_time, old_time))

    writer = BoundedJsonlWriter(
        tmp_path, "quotes", max_rows=10, max_bytes=10_000,
        retention_hours=1, max_files=10, run_id="new",
    )
    writer.close()

    assert not old.exists()
    assert writer.stats["files_expired"] == 1


def test_decay_late_quote_not_mislabelled_as_on_time():
    base = 1_700_000_000_000_000_000
    tracker = EdgeDecayTracker()
    params = _params()
    tracker.ingest(_quote("entropy", base, 101, 101), params)
    events = tracker.ingest(
        _quote("lighter-rh", base + 1_000_000, 100, 100.5), params)
    assert any(e["record_type"] == "signal" for e in events)

    # First subsequent frame only at 300ms: 50/100ms are NOT measured.
    events = tracker.ingest(
        _quote("entropy", base + 301_000_000, 101, 101), params)
    rows = {e["target_ms"]: e for e in events
            if e["record_type"] == "decay"}
    assert rows[50]["status"] == "late_quote_outside_tolerance"
    assert rows[100]["status"] == "late_quote_outside_tolerance"
    assert rows[250]["status"] == "late_quote_outside_tolerance"
    assert rows[50]["sell_edge_bps"] is None
    assert rows[50]["sampling_lag_ms"] > 200
    assert rows[50]["observed_wall_ns"] is not None
