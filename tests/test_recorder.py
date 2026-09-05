"""Minute recorder: aggregation, rollover, and generic CSV output.

Run:  python3 -m pytest tests/  (or  python3 tests/test_recorder.py)
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.recorder import HEADER, MinuteRecorder  # noqa: E402


def set_book(book, bid, ask):
    book.apply_hl([[{"px": str(bid), "sz": "10"}],
                   [{"px": str(ask), "sz": "10"}]])


def make_recorder(path, a_book, b_book):
    return MinuteRecorder(path, a_book, b_book, staleness_sec=1e9,
                          venue_a_name="entropy", venue_b_name="lighter-rh",
                          symbol="SNDK")


def test_generic_schema_and_minute_aggregation():
    a_book, b_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = make_recorder(path, a_book, b_book)

    t0 = 1_700_000_000.0            # 20s into a minute (boundary at ...020)
    # minute 1: venue A 10 bps rich, then 20 bps rich
    set_book(a_book, 100.09, 100.11)   # mid 100.10
    set_book(b_book, 99.99, 100.01)    # mid 100.00
    rec.sample(t0)
    set_book(a_book, 100.19, 100.21)   # mid 100.20
    rec.sample(t0 + 10)
    # next minute: back to 10 bps rich -> flushes minute 1
    set_book(a_book, 100.09, 100.11)
    rec.sample(t0 + 45)
    rec.close()                        # flushes the partial minute 2

    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [*rows[0]] == HEADER
    assert HEADER[:9] == ["minute_ts", "time_utc", "venue_a", "venue_b",
                          "symbol", "a_bid", "a_ask", "b_bid", "b_ask"]
    assert len(rows) == 2
    m1, m2 = rows
    assert m1["venue_a"] == "entropy" and m1["venue_b"] == "lighter-rh"
    assert m1["symbol"] == "SNDK"
    assert int(m1["samples"]) == 2 and int(m2["samples"]) == 1
    assert abs(float(m1["premium_open_bps"]) - 10.0) < 0.2
    assert abs(float(m1["premium_high_bps"]) - 20.0) < 0.2
    assert abs(float(m1["premium_close_bps"]) - 20.0) < 0.2
    assert abs(float(m1["premium_mean_bps"]) - 15.0) < 0.2
    # executable edges: sell = bid_a/ask_b - 1, buy = bid_b/ask_a - 1
    assert abs(float(m2["sell_a_edge_max_bps"])
               - ((100.09 / 100.01 - 1) * 1e4)) < 0.05
    assert abs(float(m2["buy_a_edge_max_bps"])
               - ((99.99 / 100.11 - 1) * 1e4)) < 0.05
    # closes carry the last books
    assert float(m2["a_bid"]) == 100.09
    assert float(m2["b_ask"]) == 100.01


def test_stale_books_are_skipped():
    a_book, b_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = make_recorder(path, a_book, b_book)
    rec.sample(1_700_000_000.0)        # both books empty -> nothing recorded
    set_book(a_book, 100.0, 100.02)    # only one side fresh
    rec.sample(1_700_000_001.0)
    rec.close()
    assert rec.rows_written == 0
    assert not os.path.exists(path)    # no row, no file


def test_append_keeps_single_generic_header():
    a_book, b_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    set_book(a_book, 100.0, 100.02)
    set_book(b_book, 100.0, 100.02)
    for start in (1_700_000_000.0, 1_700_000_060.0):
        rec = make_recorder(path, a_book, b_book)
        rec.sample(start)
        rec.close()
    with open(path) as fh:
        lines = fh.read().strip().splitlines()
    assert len(lines) == 3             # one header + two rows
    assert lines[0] == ",".join(HEADER)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
