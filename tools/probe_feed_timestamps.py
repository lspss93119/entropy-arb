"""One-shot, no-credentials WebSocket schema probe for P0-02.

Public subscriptions only. No order endpoints, no account keys, no persistence.
A missing feed is INCONCLUSIVE, not evidence of a matching payload.
"""
import asyncio
import json
import statistics
import time

from websockets.asyncio.client import connect

from entropy_arb.feeds import _source_time_seconds


async def inspect(name, url, *, market=None, coin=None, seconds=20):
    kind = "hl" if coin else "lighter"
    ages, missing, frames = [], 0, 0
    keys = set()
    started = time.monotonic()
    try:
        async with connect(url, open_timeout=8, close_timeout=3,
                           ping_interval=15) as ws:
            if kind == "hl":
                await ws.send(json.dumps({"method": "subscribe",
                    "subscription": {"type": "l2Book", "coin": coin}}))
            while time.monotonic() - started < seconds:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    if kind == "lighter" and not frames:
                        await ws.send(json.dumps({"type": "subscribe",
                            "channel": f"order_book/{market}"}))
                    continue
                msg = json.loads(raw)
                if kind == "lighter" and msg.get("type") == "connected":
                    await ws.send(json.dumps({"type": "subscribe",
                        "channel": f"order_book/{market}"}))
                if kind == "hl":
                    if msg.get("channel") != "l2Book" or (msg.get("data") or {}).get("coin") != coin:
                        continue
                    d = msg["data"]
                    ts = _source_time_seconds(d.get("time"), divisor=1000)
                    keys.update(d.keys())
                else:
                    if msg.get("type") not in ("subscribed/order_book", "update/order_book"):
                        continue
                    if not str(msg.get("channel", "")).endswith(f":{market}") and not str(msg.get("channel", "")).endswith(f"/{market}"):
                        continue
                    ob = msg.get("order_book") or {}
                    keys.update(f"ob.{k}" for k in ob.keys())
                    keys.update(f"frame.{k}" for k in msg.keys())
                    stamp = ob.get("last_updated_at", msg.get("last_updated_at"))
                    ts = (_source_time_seconds(stamp, divisor=1_000_000)
                          if stamp is not None else
                          _source_time_seconds(msg.get("timestamp"), divisor=1000))
                frames += 1
                if ts is None:
                    missing += 1
                else:
                    ages.append(round(time.time() - ts, 4))
                if frames >= 8:
                    break
    except Exception as exc:
        return {"venue": name, "status": "INCONCLUSIVE",
                "frames": frames, "error": type(exc).__name__ + ": " + str(exc)[:120],
                "field_keys": sorted(keys)}
    if not frames:
        return {"venue": name, "status": "INCONCLUSIVE", "frames": 0,
                "field_keys": sorted(keys)}
    return {"venue": name, "status": "OBSERVED" if not missing else "MISSING_TIMESTAMP",
            "frames": frames, "missing_source_time": missing,
            "age_sec": {"min": min(ages), "median": round(statistics.median(ages), 4),
                        "max": max(ages)} if ages else None,
            "field_keys": sorted(keys)}


async def main():
    results = await asyncio.gather(
        inspect("Hyperliquid", "wss://api.hyperliquid.xyz/ws", coin="BTC"),
        inspect("Lighter Core", "wss://mainnet.zklighter.elliot.ai/stream?readonly=true", market=0),
        inspect("Lighter RH", "wss://api.rh.lighter.xyz/stream?readonly=true", market=0))
    for record in results:
        print("P0-02 READONLY PROBE", json.dumps(record, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
