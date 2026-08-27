"""Minute recorder: aggregation, rollover, CSV output.

Run:  python3 -m pytest tests/  (or  python3 tests/test_recorder.py)
"""
import csv
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.recorder import MinuteRecorder  # noqa: E402


MINUTE_HEADER = [
    "minute_ts", "time_utc", "symbol", "hedge",
    "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
    "premium_open_bps", "premium_high_bps", "premium_low_bps",
    "premium_close_bps", "premium_mean_bps", "premium_std_bps",
    "sell_edge_mean_bps", "sell_edge_max_bps",
    "buy_edge_mean_bps", "buy_edge_max_bps", "samples",
]
SAMPLE_HEADER = [
    "timestamp_ms", "premium_bps", "sell_edge_bps", "buy_edge_bps",
]


def set_book(book, bid, ask):
    book.apply_hl([[{"px": str(bid), "sz": "10"}],
                   [{"px": str(ask), "sz": "10"}]])


def test_minute_aggregation_and_rollover():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9,
                         symbol="SNDK", hedge="lighter-rh")

    t0 = 1_700_000_000.0            # 20s into a minute (boundary at ...020)
    # minute 1: entropy 10 bps rich, then 20 bps rich
    set_book(e_book, 100.09, 100.11)   # mid 100.10
    set_book(h_book, 99.99, 100.01)    # mid 100.00
    rec.sample(t0)
    set_book(e_book, 100.19, 100.21)   # mid 100.20
    rec.sample(t0 + 10)
    # next minute: back to 10 bps rich -> flushes minute 1
    set_book(e_book, 100.09, 100.11)
    rec.sample(t0 + 45)
    rec.close()                        # flushes the partial minute 2

    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [*rows[0]] == MINUTE_HEADER
    assert len(rows) == 2
    m1, m2 = rows
    assert (m1["symbol"], m1["hedge"]) == ("SNDK", "lighter-rh")
    assert (m2["symbol"], m2["hedge"]) == ("SNDK", "lighter-rh")
    assert int(m1["samples"]) == 2 and int(m2["samples"]) == 1
    assert abs(float(m1["premium_open_bps"]) - 10.0) < 0.2
    assert abs(float(m1["premium_high_bps"]) - 20.0) < 0.2
    assert abs(float(m1["premium_close_bps"]) - 20.0) < 0.2
    assert abs(float(m1["premium_mean_bps"]) - 15.0) < 0.2
    # executable edges: sell = bid_e/ask_h - 1, buy = bid_h/ask_e - 1
    assert abs(float(m2["sell_edge_max_bps"])
               - ((100.09 / 100.01 - 1) * 1e4)) < 0.05
    assert abs(float(m2["buy_edge_max_bps"])
               - ((99.99 / 100.11 - 1) * 1e4)) < 0.05
    # closes carry the last books
    assert float(m2["entropy_bid"]) == 100.09
    assert float(m2["hedge_ask"]) == 100.01


def test_sample_rows_match_minute_bbo_math_and_timestamp():
    e_book, h_book = OrderBook(), OrderBook()
    directory = tempfile.mkdtemp()
    minute_path = os.path.join(directory, "minutes-SNDK-lighter-rh.csv")
    sample_path = os.path.join(directory, "samples-SNDK-lighter-rh.csv")
    rec = MinuteRecorder(minute_path, e_book, h_book, staleness_sec=1e9,
                         symbol="SNDK", hedge="lighter-rh")

    t0 = 1_700_000_020.123
    set_book(e_book, 100.09, 100.11)
    set_book(h_book, 99.99, 100.01)
    rec.sample(t0)
    set_book(e_book, 100.19, 100.21)
    rec.sample(t0 + 1.0)
    rec.close()

    assert os.path.exists(sample_path)
    with open(sample_path, newline="") as fh:
        sample_rows = list(csv.DictReader(fh))
    with open(minute_path, newline="") as fh:
        minute_rows = list(csv.DictReader(fh))

    assert [*sample_rows[0]] == SAMPLE_HEADER
    assert len(sample_rows) == 2
    assert int(minute_rows[0]["samples"]) == len(sample_rows)
    assert [int(r["timestamp_ms"]) for r in sample_rows] == [
        int(t0 * 1000), int((t0 + 1.0) * 1000),
    ]
    assert ((int(sample_rows[0]["timestamp_ms"]) // 60_000) * 60
            == int(minute_rows[0]["minute_ts"]))

    first = sample_rows[0]
    assert float(first["premium_bps"]) == (
        (((100.09 + 100.11) / 2) / ((99.99 + 100.01) / 2) - 1) * 1e4
    )
    assert float(first["sell_edge_bps"]) == ((100.09 / 100.01) - 1) * 1e4
    assert float(first["buy_edge_bps"]) == ((99.99 / 100.11) - 1) * 1e4


@pytest.mark.parametrize(
    ("hedge", "minute_name", "sample_name"),
    [
        ("lighter", "minutes-SNDK-lighter.csv", "samples-SNDK-lighter.csv"),
        ("lighter-rh", "minutes-SNDK-lighter-rh.csv",
         "samples-SNDK-lighter-rh.csv"),
        ("tradexyz", "minutes-SNDK-tradexyz.csv",
         "samples-SNDK-tradexyz.csv"),
    ],
)
def test_sample_file_is_sibling_of_venue_minute_file(
        hedge, minute_name, sample_name):
    e_book, h_book = OrderBook(), OrderBook()
    directory = tempfile.mkdtemp()
    minute_path = os.path.join(directory, minute_name)
    sample_path = os.path.join(directory, sample_name)
    rec = MinuteRecorder(minute_path, e_book, h_book, staleness_sec=1e9,
                         symbol="SNDK", hedge=hedge)
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)
    rec.sample(1_700_000_000.0)
    rec.close()

    assert os.path.exists(sample_path)


def test_sample_rows_flush_every_ten_and_close_flushes_remainder():
    e_book, h_book = OrderBook(), OrderBook()
    directory = tempfile.mkdtemp()
    minute_path = os.path.join(directory, "minutes-SNDK-lighter.csv")
    sample_path = os.path.join(directory, "samples-SNDK-lighter.csv")
    rec = MinuteRecorder(minute_path, e_book, h_book, staleness_sec=1e9,
                         symbol="SNDK", hedge="lighter")
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)

    def persisted_rows():
        with open(sample_path, newline="") as fh:
            return list(csv.DictReader(fh))

    try:
        for offset in range(9):
            rec.sample(1_700_000_000.0 + offset)
        assert len(persisted_rows()) == 0

        rec.sample(1_700_000_009.0)
        assert len(persisted_rows()) == 10

        rec.sample(1_700_000_010.0)
        rec.sample(1_700_000_011.0)
        assert len(persisted_rows()) == 10
    finally:
        rec.close()

    assert len(persisted_rows()) == 12


def test_stale_books_are_skipped():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9,
                         symbol="SNDK", hedge="lighter-rh")
    rec.sample(1_700_000_000.0)        # both books empty -> nothing recorded
    set_book(e_book, 100.0, 100.02)    # only one side fresh
    rec.sample(1_700_000_001.0)
    rec.close()
    assert rec.rows_written == 0
    assert not os.path.exists(path)    # no row, no file
    assert not os.path.exists(os.path.join(os.path.dirname(path),
                                           "samples.csv"))


def test_append_keeps_single_header():
    e_book, h_book = OrderBook(), OrderBook()
    path = os.path.join(tempfile.mkdtemp(), "minutes.csv")
    set_book(e_book, 100.0, 100.02)
    set_book(h_book, 100.0, 100.02)
    for start in (1_700_000_000.0, 1_700_000_060.0):
        rec = MinuteRecorder(path, e_book, h_book, staleness_sec=1e9,
                             symbol="SNDK", hedge="lighter-rh")
        rec.sample(start)
        rec.close()
    with open(path) as fh:
        lines = fh.read().strip().splitlines()
    assert len(lines) == 3             # one header + two rows
    assert lines[0].startswith("minute_ts,")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
