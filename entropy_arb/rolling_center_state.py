"""Durable last-valid rolling-center snapshot storage.

The state is intentionally separate from the market-history samples.  It is
one small JSON document written atomically so a restart can reuse a recent
valid center without loading the entire history database.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path

from .strategy import RollingCenterSnapshot


class RollingCenterStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> RollingCenterSnapshot | None:
        try:
            with self.path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
            if not isinstance(raw, dict):
                return None
            raw_sample_count = raw["sample_count"]
            if (
                isinstance(raw_sample_count, bool)
                or int(raw_sample_count) != raw_sample_count
            ):
                return None
            snapshot = RollingCenterSnapshot(
                center_bps=float(raw["center_bps"]),
                calculated_at=float(raw["calculated_at"]),
                window_start=float(raw["window_start"]),
                window_end=float(raw["window_end"]),
                coverage_ratio=float(raw["coverage_ratio"]),
                sample_count=int(raw_sample_count),
            )
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None
        values = (
            snapshot.center_bps,
            snapshot.calculated_at,
            snapshot.window_start,
            snapshot.window_end,
            snapshot.coverage_ratio,
        )
        if (
            not all(math.isfinite(value) for value in values)
            or snapshot.sample_count <= 0
            or not 0.0 <= snapshot.coverage_ratio <= 1.0
            or snapshot.window_start > snapshot.window_end
        ):
            return None
        return snapshot

    def save(self, snapshot: RollingCenterSnapshot) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "center_bps": snapshot.center_bps,
            "calculated_at": snapshot.calculated_at,
            "window_start": snapshot.window_start,
            "window_end": snapshot.window_end,
            "coverage_ratio": snapshot.coverage_ratio,
            "sample_count": snapshot.sample_count,
        }
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
