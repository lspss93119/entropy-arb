#!/usr/bin/env python3
"""Read-only public-book timing probe. No keys, account calls or orders."""
import argparse
import asyncio
import json
import math
import statistics
import time

from entropy_arb.book import OrderBook
from entropy_arb.feeds import HLBookFeed, LighterBookFeed


def summary(values):
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {"count": len(values), "p50": round(ordered[int((len(ordered)-1)*0.50)], 3),
            "p95": round(ordered[int((len(ordered)-1)*0.95)], 3),
            "p99": round(ordered[int((len(ordered)-1)*0.99)], 3),
            "max": round(ordered[-1], 3)}


async def observe(feed, book, seconds, max_quote_age):
    stop = asyncio.Event()
    age_ms, gaps_ms = [], []
    counts = {"updates": 0, "missing_source_ts": 0, "future_source_ts": 0,
              "stale_at_event": 0}
    last_update = None
    def notify():
        nonlocal last_update
        if not book.ready:
            return
        now = time.time()
        counts["updates"] += 1
        if last_update is not None:
            gaps_ms.append((time.monotonic()-last_update)*1000)
        last_update = time.monotonic()
        if book.source_ts is None:
            counts["missing_source_ts"] += 1
        else:
            age = (now-book.source_ts)*1000
            if math.isfinite(age):
                age_ms.append(age)
                if age < 0:
                    counts["future_source_ts"] += 1
        if max_quote_age > 0 and not book.is_fresh(15, max_quote_age):
            counts["stale_at_event"] += 1
    feed.notify = notify
    task = asyncio.create_task(feed.run(stop))
    try:
        await asyncio.sleep(seconds)
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return {"seconds": seconds, "counts": counts,
            "event_time_minus_source_ms": summary(age_ms),
            "inter_update_gap_ms": summary(gaps_ms),
            "notes": "Age uses local wall clock, not one-way network latency; check NTP."
            " Quiet markets may legitimately have fewer updates."}


async def main(args):
    book = OrderBook()
    if args.venue == "lighter":
        feed = LighterBookFeed("probe", args.ws_url or
            "wss://mainnet.zklighter.elliot.ai/stream", args.market_id, book,
            lambda: None)
    else:
        feed = HLBookFeed("probe", args.ws_url or
            "wss://api.hyperliquid.xyz/ws", args.coin, book, lambda: None)
    report = await observe(feed, book, args.seconds, args.max_quote_age)
    report.update({"venue": args.venue,
                   "market_id": args.market_id if args.venue=="lighter" else None,
                   "coin": args.coin if args.venue=="hl" else None})
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Public websocket order books ONLY.")
    parser.add_argument("--venue", choices=("lighter", "hl"), required=True)
    parser.add_argument("--market-id", type=int, default=1)
    parser.add_argument("--coin", default="BTC")
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--max-quote-age", type=float, default=0)
    parser.add_argument("--ws-url", default=None)
    args=parser.parse_args()
    if args.seconds < 1 or args.seconds > 3600:
        parser.error("--seconds must be 1..3600")
    asyncio.run(main(args))
