#!/usr/bin/env python3
"""Opt-in public SNDK x Lighter RH quote latency capture.

This command never loads credentials and never starts ``main.py`` or the
trading Engine.  It is intentionally explicit about the only supported pair.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Support ``python3 tools/record_sndk_rh_quote_latency.py`` without requiring
# callers to set PYTHONPATH manually.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from entropy_arb.config import HL_WS_URL, LIGHTER_PROFILES
from entropy_arb.quote_latency_recorder import QuoteLatencyRecorder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="record-only public SNDK x Lighter RH quote latency")
    parser.add_argument("--symbol", required=True, choices=["SNDK"])
    parser.add_argument("--hedge", required=True, choices=["lighter-rh"])
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--duration-sec", type=float, default=60.0)
    parser.add_argument("--rolling-csv", default="")
    parser.add_argument("--fees-bps", type=float, default=0.0)
    parser.add_argument("--sell-quantile", type=float, default=0.80)
    parser.add_argument("--buy-quantile", type=float, default=0.50)
    parser.add_argument("--depth-levels", type=int, default=5)
    parser.add_argument("--pair-max-skew-ms", type=float, default=250.0)
    parser.add_argument("--queue-size", type=int, default=10_000)
    parser.add_argument("--max-file-rows", type=int, default=50_000)
    parser.add_argument("--max-file-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--retention-hours", type=float, default=24.0)
    parser.add_argument("--max-files", type=int, default=24)
    parser.add_argument("--hl-ws-url", default=HL_WS_URL)
    parser.add_argument("--rh-ws-url", default=LIGHTER_PROFILES["lighter-rh"].ws_url)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = Path(args.output_dir) / run_id
    recorder = QuoteLatencyRecorder(
        run_dir, symbol=args.symbol, hedge=args.hedge,
        hl_ws_url=args.hl_ws_url, rh_ws_url=args.rh_ws_url,
        rolling_csv=args.rolling_csv, fees_bps=args.fees_bps,
        sell_quantile=args.sell_quantile, buy_quantile=args.buy_quantile,
        depth_levels=args.depth_levels,
        pair_max_skew_ms=args.pair_max_skew_ms,
        queue_size=args.queue_size, max_file_rows=args.max_file_rows,
        max_file_bytes=args.max_file_bytes,
        retention_hours=args.retention_hours, max_files=args.max_files,
    )
    manifest = asyncio.run(recorder.run(args.duration_sec))
    print(json.dumps({"run_dir": str(run_dir), "manifest": manifest},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
