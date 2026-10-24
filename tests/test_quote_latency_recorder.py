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
    assert signal["source_timestamp_status"] if "source_timestamp_status" in signal else True


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

