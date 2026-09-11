"""Low-volume feed telemetry for latency and quote-age research."""
import csv
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import entropy_arb.book as book_module  # noqa: E402
from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.feeds import HLBookFeed  # noqa: E402
from entropy_arb.recorder import MinuteRecorder  # noqa: E402


def _levels(bid=100.0, ask=100.02):
    return [[{"px": str(bid), "sz": "10"}],
            [{"px": str(ask), "sz": "10"}]]


def _set_book(book, bid=100.0, ask=100.02, server_ts_ms=None):
    book.apply_hl(_levels(bid, ask), server_ts_ms=server_ts_ms)


def test_orderbook_tracks_update_gaps_and_hyperliquid_server_age(monkeypatch):
    clock = iter((1000.0, 1000.5, 1001.5))
    monkeypatch.setattr(book_module.time, "time", lambda: next(clock))
    book = OrderBook()

    _set_book(book, server_ts_ms=999_900)
    _set_book(book, server_ts_ms=1_000_400)
    _set_book(book, server_ts_ms=1_001_400)

    stats = book.drain_feed_stats()
    assert stats["update_count"] == 3
    assert stats["gap_p50_ms"] == 750.0
    assert stats["gap_p95_ms"] == 975.0
    assert stats["age_p95_ms"] == 100.0
    assert book.last_update_gap_ms == 1000.0
    assert book.server_age_ms(1001.5) == 100.0


def test_hyperliquid_feed_preserves_server_timestamp():
    book = OrderBook()
    feed = HLBookFeed("ENTROPY", "wss://example.invalid", "io:SNDK",
                      book, lambda: None)

    feed._on_frame({
        "channel": "l2Book",
        "data": {"coin": "io:SNDK", "time": 1_700_000_000_123,
                  "levels": _levels()},
    })

    assert book.last_server_ts_ms == 1_700_000_000_123


def test_minute_row_contains_feed_cadence_stats(tmp_path):
    entropy_book, hedge_book = OrderBook(), OrderBook()
    path = str(tmp_path / "minutes.csv")
    recorder = MinuteRecorder(
        path, entropy_book, hedge_book, staleness_sec=1e9,
        symbol="SNDK", hedge="lighter-rh")

    t0 = 1_700_000_000.0
    recorder.sample(t0)
    server_ts = int(time.time() * 1000) - 200
    _set_book(entropy_book, server_ts_ms=server_ts)
    _set_book(hedge_book)
    recorder.sample(t0 + 1)
    _set_book(entropy_book, bid=100.01, ask=100.03,
              server_ts_ms=int(time.time() * 1000) - 200)
    _set_book(hedge_book, bid=100.01, ask=100.03)
    recorder.sample(t0 + 2)
    recorder.sample(t0 + 60)
    recorder.close()

    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    row = rows[0]
    assert int(row["entropy_update_count"]) == 2
    assert int(row["hedge_update_count"]) == 2
    assert float(row["entropy_gap_p95_ms"]) >= 0.0
    assert float(row["hedge_gap_p95_ms"]) >= 0.0
    assert float(row["entropy_age_p95_ms"]) >= 0.0
