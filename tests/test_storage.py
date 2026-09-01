import sqlite3
from pathlib import Path

from entropy_arb.storage import MarketHistoryStore, MinuteRow, SampleRow


def sample(ts: int = 1_700_000_000_000, premium: float = 10.0) -> SampleRow:
    return SampleRow(
        timestamp_ms=ts,
        symbol="SNDK",
        hedge="lighter-rh",
        premium_bps=premium,
        sell_edge_bps=8.0,
        buy_edge_bps=-12.0,
        entropy_bid=100.09,
        entropy_ask=100.11,
        hedge_bid=99.99,
        hedge_ask=100.01,
        entropy_book_update_ms=ts - 500,
        hedge_book_update_ms=ts - 300,
    )


def minute(ts: int = 1_699_999_980) -> MinuteRow:
    return MinuteRow(
        minute_ts=ts,
        symbol="SNDK",
        hedge="lighter-rh",
        entropy_bid=100.09,
        entropy_ask=100.11,
        hedge_bid=99.99,
        hedge_ask=100.01,
        premium_open_bps=10.0,
        premium_high_bps=20.0,
        premium_low_bps=10.0,
        premium_close_bps=20.0,
        premium_mean_bps=15.0,
        premium_std_bps=5.0,
        sell_edge_mean_bps=13.0,
        sell_edge_max_bps=18.0,
        buy_edge_mean_bps=-17.0,
        buy_edge_max_bps=-12.0,
        samples=2,
    )


def test_fresh_database_creates_schema_and_meta(tmp_path: Path):
    db = tmp_path / "market-history.sqlite"
    store = MarketHistoryStore(db)
    store.close()

    with sqlite3.connect(db) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"meta", "samples", "minutes", "entropy_reference", "hedge_reference"} <= tables
        assert conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone() == ("1",)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_second_open_is_idempotent(tmp_path: Path):
    db = tmp_path / "market-history.sqlite"
    MarketHistoryStore(db).close()
    MarketHistoryStore(db).close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_exact_duplicate_is_noop_and_conflicting_payload_does_not_replace(tmp_path: Path):
    db = tmp_path / "market-history.sqlite"
    store = MarketHistoryStore(db)
    store.append_sample(sample())
    first = store.flush()
    assert first.ok
    assert first.datasets["samples"].inserted == 1

    store.append_sample(sample())
    duplicate = store.flush()
    assert duplicate.datasets["samples"].duplicates == 1

    store.append_sample(sample(premium=999.0))
    conflict = store.flush()
    assert conflict.datasets["samples"].conflicts == 1
    store.close()

    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT premium_bps FROM samples WHERE symbol=? AND hedge=? AND timestamp_ms=?",
            ("SNDK", "lighter-rh", 1_700_000_000_000),
        ).fetchone() == (10.0,)


def test_flush_transaction_rolls_back_all_datasets_and_keeps_buffers(tmp_path: Path):
    db = tmp_path / "market-history.sqlite"
    store = MarketHistoryStore(db)
    store._conn.execute(
        "CREATE TRIGGER fail_minutes BEFORE INSERT ON minutes "
        "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
    )
    store.append_sample(sample())
    store.append_minute(minute())

    report = store.flush()
    assert not report.ok
    assert store.pending_rows["samples"] == 1
    assert store.pending_rows["minutes"] == 1

    store._conn.execute("DROP TRIGGER fail_minutes")
    assert store.flush().ok
    store.close()

    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM minutes").fetchone() == (1,)
