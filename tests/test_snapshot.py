import multiprocessing as mp
import sqlite3
import time
from pathlib import Path

import pytest

from entropy_arb.snapshot import create_snapshot
from entropy_arb.storage import MarketHistoryStore, SampleRow


def sample(ts: int) -> SampleRow:
    return SampleRow(
        timestamp_ms=ts,
        symbol="SNDK",
        hedge="lighter-rh",
        premium_bps=0.0,
        sell_edge_bps=0.0,
        buy_edge_bps=0.0,
        entropy_bid=100.0,
        entropy_ask=100.1,
        hedge_bid=100.0,
        hedge_ask=100.1,
        entropy_book_update_ms=ts,
        hedge_book_update_ms=ts,
    )


def _snapshot_writer(db_path: str, ready, stop) -> None:
    store = MarketHistoryStore(db_path)
    i = 0
    try:
        while not stop.is_set():
            store.append_sample(sample(1_700_000_000_000 + i))
            report = store.flush()
            if not report.ok:
                raise RuntimeError("writer flush failed")
            ready.set()
            i += 1
            time.sleep(0.01)
    finally:
        store.close()


def test_snapshot_is_standalone_and_quick_check_ok(tmp_path: Path):
    source = tmp_path / "live.sqlite"
    destination = tmp_path / "snapshot.sqlite"
    store = MarketHistoryStore(source)
    store.append_sample(sample(1_700_000_000_000))
    assert store.flush().ok
    create_snapshot(source, destination)
    store.close()

    assert destination.exists()
    with sqlite3.connect(destination) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone() == (1,)


def test_snapshot_is_consistent_while_writer_is_active(tmp_path: Path):
    source = tmp_path / "live.sqlite"
    destination = tmp_path / "snapshot.sqlite"
    MarketHistoryStore(source).close()
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    stop = ctx.Event()
    writer = ctx.Process(target=_snapshot_writer, args=(str(source), ready, stop))
    writer.start()
    try:
        assert ready.wait(10)
        create_snapshot(source, destination)
    finally:
        stop.set()
        writer.join(10)
    assert writer.exitcode == 0

    with sqlite3.connect(destination) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] >= 1


def test_destination_already_exists_is_rejected_without_overwrite(tmp_path: Path):
    source = tmp_path / "live.sqlite"
    destination = tmp_path / "snapshot.sqlite"
    MarketHistoryStore(source).close()
    destination.write_bytes(b"sentinel")

    with pytest.raises(FileExistsError):
        create_snapshot(source, destination)

    assert destination.read_bytes() == b"sentinel"


def test_missing_source_is_rejected(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        create_snapshot(tmp_path / "missing.sqlite", tmp_path / "snapshot.sqlite")
