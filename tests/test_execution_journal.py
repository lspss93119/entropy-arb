"""Contract tests for the optional journal; no exchange credentials needed."""
import asyncio
import json
from pathlib import Path

from entropy_arb.execution_journal import ExecutionJournal, stamp


def test_disabled_no_file(tmp_path: Path):
    async def scenario():
        j = ExecutionJournal(str(tmp_path / "events.jsonl"))
        j.start()
        j.emit("decision", execution_id="a", side="buy")
        await j.close()
        assert not (tmp_path / "events.jsonl").exists()
    asyncio.run(scenario())


def test_events_written_with_identifiers(tmp_path: Path):
    async def scenario():
        p = tmp_path / "events.jsonl"
        j = ExecutionJournal(str(p), enabled=True)
        j.start()
        j.emit("decision", execution_id="e1", direction="sell_entropy")
        j.emit("order_result", execution_id="e1", leg_id="lighter", status="filled")
        await j.close()
        lines = [json.loads(line) for line in p.read_text().splitlines()]
        assert len(lines) == 2
        assert {r["event_type"] for r in lines} == {"decision", "order_result"}
        assert len({r["event_id"] for r in lines}) == 2
        assert all(r["run_id"] == j.run_id and r["schema_version"] == 1 for r in lines)
        assert all(r["ts_utc_ns"] > 0 and r["monotonic_ns"] > 0 for r in lines)
        assert j.written == 2 and j.dropped == 0
    asyncio.run(scenario())


def test_bounded_queue_reports_loss(tmp_path: Path):
    async def scenario():
        j = ExecutionJournal(str(tmp_path / "events.jsonl"), enabled=True, max_queue=1)
        j.start()
        for _ in range(100):
            j.emit("decision")
        assert j.dropped > 0
        await j.close()
    asyncio.run(scenario())


def test_stamp_tracks_time():
    trace = {}
    stamp(trace, "submit")
    assert trace["submit_mono_ns"] > 0
    assert trace["submit_utc_ns"] > 0
    stamp(None, "submit")


def test_write_failure_is_counted_without_blocking_close(tmp_path: Path):
    async def scenario():
        j = ExecutionJournal(str(tmp_path / "events.jsonl"), enabled=True)

        def fail_append(path, events):
            raise OSError("disk full")

        j._append = fail_append
        j.start()
        j.emit("decision", execution_id="e1")
        await j.close()
        return j

    j = asyncio.run(scenario())
    assert j.written == 0
    assert j.write_errors == 1
    assert j.dropped == 0


def test_journal_can_restart_after_clean_close(tmp_path: Path):
    async def scenario():
        p = tmp_path / "events.jsonl"
        j = ExecutionJournal(str(p), enabled=True)
        j.start()
        j.emit("start", execution_id="first")
        await j.close()
        first_run = j.run_id
        j.start()
        j.emit("restart", execution_id="second")
        await j.close()
        return first_run, j.run_id, [json.loads(line) for line in p.read_text().splitlines()]

    first_run, second_run, events = asyncio.run(scenario())
    assert first_run == second_run
    assert [event["event_type"] for event in events] == ["start", "restart"]
    assert all(event["run_id"] == first_run for event in events)
