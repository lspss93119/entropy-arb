"""Optional, best-effort execution event journal.

Never await disk I/O from the order path. This journal is an observability
channel, not a source of truth for exchange settlement or account state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any, Optional

log = logging.getLogger(__name__)


def stamp(trace: Optional[dict], name: str) -> None:
    """Capture local observation time, not exchange execution time."""
    if trace is not None:
        trace[name + "_mono_ns"] = time.monotonic_ns()
        trace[name + "_utc_ns"] = time.time_ns()


class ExecutionJournal:
    def __init__(self, path: str, *, enabled: bool = False,
                 max_queue: int = 4096, batch_size: int = 64) -> None:
        self.enabled = enabled
        self.path = path
        self.max_queue = max_queue
        self.batch_size = batch_size
        self.run_id = str(uuid.uuid4())
        self.queue: Optional[asyncio.Queue] = None
        self.worker: Optional[asyncio.Task] = None
        self.accepted = 0
        self.written = 0
        self.dropped = 0
        self.write_errors = 0

    def start(self) -> None:
        if not self.enabled or self.worker is not None:
            return
        self.queue = asyncio.Queue(maxsize=self.max_queue)
        self.worker = asyncio.create_task(self._write_loop(), name="execution-journal")

    def emit(self, event_type: str, *, execution_id: Optional[str] = None,
             leg_id: Optional[str] = None, **fields: Any) -> None:
        if not self.enabled:
            return
        try:
            if self.queue is None or self.worker is None or self.worker.done():
                self.dropped += 1
                return
            event = {
                "schema_version": 1, "run_id": self.run_id,
                "event_id": str(uuid.uuid4()), "event_type": event_type,
                "execution_id": execution_id, "leg_id": leg_id,
                "ts_utc_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                **fields,
            }
            self.queue.put_nowait(event)
            self.accepted += 1
        except (asyncio.QueueFull, Exception):
            self.dropped += 1

    @staticmethod
    def _append(path: str, events: list[dict]) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, separators=(",", ":"), allow_nan=False) + "\n")

    async def _write_loop(self) -> None:
        assert self.queue is not None
        q = self.queue
        while True:
            first = await q.get()
            if first is None:
                q.task_done()
                break
            batch = [first]
            while len(batch) < self.batch_size:
                try:
                    nxt = q.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if nxt is None:
                    q.task_done()
                    break
                batch.append(nxt)
            try:
                await asyncio.to_thread(self._append, self.path, batch)
                self.written += len(batch)
            except Exception:
                self.write_errors += len(batch)
                log.exception("execution journal write failed (%d events)", len(batch))
            finally:
                for _ in batch:
                    q.task_done()

    async def close(self, timeout: float = 5.0) -> None:
        if self.worker is None or self.queue is None:
            return
        try:
            await asyncio.wait_for(self.queue.join(), timeout=timeout)
            await asyncio.wait_for(self.queue.put(None), timeout=timeout)
            await asyncio.wait_for(self.worker, timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("execution journal shutdown timed out; events may be missing")
            self.worker.cancel()
        except Exception:
            log.exception("execution journal close failed")
        finally:
            self.worker = None
            self.queue = None
