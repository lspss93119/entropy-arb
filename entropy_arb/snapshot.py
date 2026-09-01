"""Create consistent standalone SQLite market-history snapshots."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def create_snapshot(source: Path, destination: Path) -> Path:
    """Back up a live market-history database into a verified standalone file."""
    source = Path(source)
    destination = Path(destination)
    if not source.exists():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)

    src = sqlite3.connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise RuntimeError(f"snapshot quick_check failed: {result!r}")
        dst.commit()
    except BaseException:
        dst.close()
        src.close()
        destination.unlink(missing_ok=True)
        raise
    else:
        dst.close()
        src.close()
        return destination
