"""Standalone quote-latency recorder tests with no network or credentials."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from entropy_arb.quote_latency_recorder import QuoteLatencyRecorder


def _params():
    return SimpleNamespace(
        valid=True, effective_at=1_700_000_000,
        window_start=1_699_971_200, window_end=1_700_000_000,
        sample_count=480, coverage=1.0,
        midline_bps=0.0, upper_bps=2.0, lower_bps=3.0,
        invalid_reason=None,
    )


def _hl_message():
    return {
        "channel": "l2Book",
        "data": {"coin": "io:SNDK", "time": 1_700_000_000_000,
                 "levels": [[{"px": "101", "sz": "5"}],
                            [{"px": "101", "sz": "5"}]]},
    }


def _rh_message():
    return {
        "type": "subscribed/order_book", "channel": "order_book/32",
        "order_book": {"nonce": 1,
                        "bids": [{"price": "100", "size": "5"}],
                        "asks": [{"price": "100.5", "size": "5"}]},
    }


class FakeFeed:
    def __init__(self, venue, update, lifecycle, book):
        self.venue = venue
        self.update = update
        self.lifecycle = lifecycle
        self.book = book

    async def run(self, stop):
        self.lifecycle(self.venue, "connected", 1_700_000_000_000_000_000,
                       10, 1)
        if self.venue == "entropy":
            msg = _hl_message()
            self.book.apply_hl(msg["data"]["levels"])
            self.update(self.venue, "snapshot", msg,
                        1_700_000_000_000_000_000, 10, 1)
        else:
            msg = _rh_message()
            self.book.apply_lighter(msg["order_book"], snapshot=True)
            self.update(self.venue, "snapshot", msg,
                        1_700_000_000_001_000_000, 11, 1)
        await stop.wait()
        self.lifecycle(self.venue, "disconnected", 1_700_000_000_002_000_000,
                       12, 1)


def fake_factory(update, lifecycle, books):
    return [
        FakeFeed("entropy", update, lifecycle, books["entropy"]),
        FakeFeed("lighter-rh", update, lifecycle, books["lighter-rh"]),
    ]


def test_recorder_requires_explicit_sndk_rh_pair(tmp_path):
    with pytest.raises(ValueError, match="SNDK"):
        QuoteLatencyRecorder(tmp_path, symbol="OAI", hedge="lighter-rh")
    with pytest.raises(ValueError, match="lighter-rh"):
        QuoteLatencyRecorder(tmp_path, symbol="SNDK", hedge="lighter")


def test_fake_run_writes_quotes_signals_decay_and_manifest(tmp_path):
    recorder = QuoteLatencyRecorder(
        tmp_path, symbol="SNDK", hedge="lighter-rh",
        rolling_provider=SimpleNamespace(refresh=lambda now: _params()),
        feed_factory=fake_factory, max_file_rows=100,
    )

    manifest = asyncio.run(recorder.run(0.01))

    assert manifest["market"] == "io:SNDK|lighter-rh:SNDK"
    assert manifest["market_ids"] == {"entropy": "io:SNDK", "lighter-rh": 32}
    assert manifest["dropped_updates"] == 0
    assert manifest["lifecycle_events"] == 4
    assert manifest["source_timestamp_counts"] == {
        "present": 1, "missing": 1, "invalid": 0,
    }
    assert (tmp_path / "manifest.json").exists()
    assert list(tmp_path.glob("quotes-*.jsonl"))
    assert list(tmp_path.glob("signals-*.jsonl"))
    assert list(tmp_path.glob("decay-*.jsonl"))
    lifecycle = list(tmp_path.glob("lifecycle-*.jsonl"))[0]
    assert sum(1 for _ in lifecycle.open()) == 4
    signals = list(tmp_path.glob("signals-*.jsonl"))[0]
    signal = json.loads(signals.read_text().splitlines()[0])
    assert signal["midline_bps"] == 0.0
    assert signal["upper_bps"] == 2.0
    assert signal["lower_bps"] == 3.0
    quotes = list(tmp_path.glob("quotes-*.jsonl"))[0]
    quote_rows = [json.loads(line) for line in quotes.read_text().splitlines()]
    assert {row["source_timestamp_status"] for row in quote_rows} == {
        "present", "missing",
    }


def test_queue_pressure_is_counted_without_blocking(tmp_path):
    recorder = QuoteLatencyRecorder(
        tmp_path, symbol="SNDK", hedge="lighter-rh", queue_size=1,
    )
    recorder._queue.put_nowait(("sentinel", None))

    recorder._enqueue(("update", None), kind="update")

    assert recorder.dropped_updates == 1


def test_cli_help_works_when_invoked_from_tools_path():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "tools/record_sndk_rh_quote_latency.py", "--help"],
        cwd=root, capture_output=True, text=True,
    )

    assert result.returncode == 0
    assert "record-only public SNDK" in result.stdout


def test_run_rebinds_queue_created_before_asyncio_run(tmp_path):
    recorder = QuoteLatencyRecorder(
        tmp_path, symbol="SNDK", hedge="lighter-rh",
        rolling_provider=SimpleNamespace(refresh=lambda now: _params()),
        feed_factory=fake_factory,
    )

    async def bind_to_another_loop():
        task = asyncio.create_task(recorder._queue.get())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    other_loop = asyncio.new_event_loop()
    try:
        other_loop.run_until_complete(bind_to_another_loop())
    finally:
        other_loop.close()

    manifest = asyncio.run(recorder.run(0.01))

    assert manifest["record_only"] is True


def test_quote_rows_are_bounded_without_full_raw_frame():
    from entropy_arb.quote_latency import normalize_feed_update
    from entropy_arb.book import OrderBook
    book = OrderBook()
    msg = _hl_message()
    msg["data"]["extra_diagnostic_payload"] = "X" * 10_000
    book.apply_hl(msg["data"]["levels"])
    quote = normalize_feed_update("entropy", "snapshot", msg,
                                  1_700_000_000_100_000_000, 123, 1, book)
    row = QuoteLatencyRecorder._quote_record(quote, msg, _params())
    assert "raw_frame" not in row
    assert row["bids"] == [[101.0, 5.0]]
    assert "X" * 100 not in json.dumps(row)


def test_expire_completed_previous_runs_only(tmp_path):
    from tools.record_sndk_rh_quote_latency import expire_previous_runs
    import time

    old = tmp_path / "old-run"
    old.mkdir()
    (old / "quotes-0001.jsonl").write_text("{}\n")
    (old / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "record_only": True,
        "market": "io:SNDK|lighter-rh:SNDK",
        "ended_wall_ns": time.time_ns() - int(48 * 3600 * 1e9),
    }))
    active = tmp_path / "active-run"
    active.mkdir()
    (active / "quotes-0001.jsonl").write_text("{}\n")
    unrelated = tmp_path / "unrelated-run"
    unrelated.mkdir()
    (unrelated / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "record_only": True,
        "market": "BTC",
        "ended_wall_ns": time.time_ns() - int(48 * 3600 * 1e9),
    }))
    assert expire_previous_runs(tmp_path, 24) == 1
    assert not old.exists()
    assert active.exists()
    assert unrelated.exists()


def test_raw_preservation_skips_cross_run_cleanup(tmp_path):
    from tools.record_sndk_rh_quote_latency import expire_previous_runs_if_allowed

    old = tmp_path / "old-run"
    old.mkdir()
    (old / "manifest.json").write_text(json.dumps({
        "schema_version": 1,
        "record_only": True,
        "market": "io:SNDK|lighter-rh:SNDK",
        "ended_wall_ns": 1,
    }))

    assert expire_previous_runs_if_allowed(tmp_path, 1, preserve_raw=True) == 0
    assert old.exists()


def test_raw_preservation_disables_writer_eviction(tmp_path):
    from entropy_arb.quote_latency import BoundedJsonlWriter

    writer = BoundedJsonlWriter(
        tmp_path, "quotes", max_rows=1, max_bytes=1024,
        retention_hours=0, max_files=1, preserve_files=True,
        run_id="run02",
    )
    writer.append({"n": 1})
    writer.append({"n": 2})
    writer.close()

    files = sorted(tmp_path.glob("quotes-*.jsonl"))
    assert len(files) == 2
    assert writer.stats["files_expired"] == 0
    assert [json.loads(line)["n"] for path in files for line in path.open()] == [1, 2]


def test_recorder_manifest_declares_raw_preservation(tmp_path):
    recorder = QuoteLatencyRecorder(
        tmp_path, symbol="SNDK", hedge="lighter-rh", preserve_raw=True,
        rolling_provider=SimpleNamespace(refresh=lambda now: _params()),
        feed_factory=fake_factory, max_file_rows=100,
    )

    manifest = asyncio.run(recorder.run(0.01))

    assert manifest["preserve_raw"] is True
    assert all(stats["files_expired"] == 0
               for stats in manifest["writer_stats"].values())
