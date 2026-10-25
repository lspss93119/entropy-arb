#!/usr/bin/env python3
"""Read-only diagnosis of missing completed minute rows for Rolling Legacy."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


def audit(path: Path, effective_at: int) -> dict:
    """Audit the exact causal [T-8h, T) window without filling gaps."""
    if effective_at % 3600:
        raise ValueError("effective_at must be UTC-hour aligned")
    start = effective_at - 8 * 3600
    rows = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            try:
                ts = int(row["minute_ts"])
            except (KeyError, ValueError, TypeError):
                continue
            if start <= ts < effective_at:
                rows.append((ts, row.get("samples")))
    counter = Counter(ts for ts, _ in rows)
    missing = [ts for ts in range(start, effective_at, 60) if ts not in counter]
    duplicate = sorted(ts for ts, count in counter.items() if count > 1)
    low_samples = sorted(ts for ts, samples in rows
                         if samples is None or not samples.isdigit()
                         or int(samples) < 10)
    return {
        "effective_at_utc": datetime.fromtimestamp(
            effective_at, timezone.utc).isoformat(),
        "rows": len(rows),
        "unique_minutes": len(counter),
        "missing_minute_utc": [
            datetime.fromtimestamp(ts, timezone.utc).isoformat()
            for ts in missing],
        "duplicate_minute_utc": [
            datetime.fromtimestamp(ts, timezone.utc).isoformat()
            for ts in duplicate],
        "low_sample_minute_utc": [
            datetime.fromtimestamp(ts, timezone.utc).isoformat()
            for ts in low_samples],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--effective-at", type=int, required=True,
                        help="UTC hour boundary as Unix seconds")
    args = parser.parse_args()
    print(json.dumps(audit(args.csv, args.effective_at), indent=2))


if __name__ == "__main__":
    main()
