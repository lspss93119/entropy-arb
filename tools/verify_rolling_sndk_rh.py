#!/usr/bin/env python3
"""Read-only verification of a dedicated Legacy Rolling recorder CSV.

No credentials, orders, live Engine, or filesystem writes.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone

from entropy_arb.rolling_legacy_strategy import LegacyRollingStrategy


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Dedicated SNDK × RH minute CSV")
    parser.add_argument("--market-key", default="io:SNDK|lighter-rh:SNDK")
    parser.add_argument("--effective-at", help="UTC ISO8601 hour boundary, e.g. 2026-10-20T11:00:00Z; defaults to current UTC hour")
    parser.add_argument("--sell-quantile", type=float, default=0.80)
    parser.add_argument("--buy-quantile", type=float, default=0.50)
    parser.add_argument("--fees-bps", type=float, default=0.0, help="Sum of both legs' configured fees in bps")
    args = parser.parse_args()

    if args.effective_at:
        try:
            iso = args.effective_at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:
                raise ValueError("expected explicit UTC timestamp")
            effective_at = int(dt.timestamp())
        except ValueError as exc:
            parser.error(f"invalid --effective-at: {exc}")
    else:
        effective_at = int(time.time() // 3600) * 3600

    if effective_at % 3600:
        parser.error("--effective-at must be a UTC whole hour")
    if args.market_key != "io:SNDK|lighter-rh:SNDK":
        parser.error("this SNDK × RH verifier requires market-key io:SNDK|lighter-rh:SNDK")
    if not math.isfinite(args.fees_bps) or not 0 <= args.fees_bps < 1000:
        parser.error("--fees-bps must be finite and non-negative")

    provider = LegacyRollingStrategy(
        csv_path=args.csv,
        market_key=args.market_key,
        sell_quantile=args.sell_quantile,
        buy_quantile=args.buy_quantile,
        fees_bps=args.fees_bps,
    )
    result = provider.refresh(effective_at)
    print(json.dumps({
        "pair": args.market_key,
        "csv": args.csv,
        "identity": "CSV market identity checked if column present; otherwise file provenance unverified",
        "effective_at": datetime.fromtimestamp(effective_at, timezone.utc).isoformat(),
        "window_start": datetime.fromtimestamp(result.window_start, timezone.utc).isoformat(),
        "window_end_exclusive": datetime.fromtimestamp(result.window_end, timezone.utc).isoformat(),
        "expected_minute_rows": 480,
        "rows": result.sample_count,
        "coverage": result.coverage,
        "valid": result.valid,
        "reason": result.invalid_reason,
        "midline_bps": result.midline_bps,
        "upper_bps": result.upper_bps,
        "lower_bps": result.lower_bps,
        "threshold_sell_bps": (result.midline_bps + result.upper_bps) if result.valid else None,
        "threshold_buy_edge_bps": (result.lower_bps - result.midline_bps) if result.valid else None,
        "mode": "offline_non_trading",
    }, indent=2, ensure_ascii=False))
    return 0 if result.valid else 2


if __name__ == "__main__":
    sys.exit(main())
