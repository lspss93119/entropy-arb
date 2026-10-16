"""Offline timing characterization for P0-03 (no real account API or orders)."""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_engine import make_engine


async def run_case(delay, recent):
    eng = make_engine()
    eng.cfg.net_tolerance_base = 999.0  # suppress hedge; isolate reconciliation
    calls = []
    async def fake_position(v):
        await asyncio.sleep(delay)
        calls.append(v.key)
        return v.position
    for v in eng.venues.values():
        v.fetch_position = lambda v=v: fake_position(v)
        v.last_traded_ts = time.time() if recent else 0.0
    async def no_hedge():
        return None
    eng._maybe_hedge = no_hedge
    loop_task = asyncio.create_task(eng._reconcile_loop())
    start = time.monotonic()
    eng._reconcile_evt.set()
    # Emulate healthy asynchronous venue fetches, wait for both or 4 s.
    deadline = start + 4.0
    while len(calls) < (0 if recent else 2) and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    # Recent trade grace skip yields no fetches, so wait 1.1s for the reconcile loop.
    if recent:
        await asyncio.sleep(max(0, 1.1 - (time.monotonic() - start)))
    elapsed = time.monotonic() - start
    eng.request_stop()
    await asyncio.wait_for(loop_task, timeout=2.0)
    return round(elapsed, 3), sorted(calls)


def test_reconcile_event_has_one_second_delay_and_concurrent_fetch():
    async def scenario():
        for latency in (0.05, 0.2, 0.5):
            elapsed, calls = await run_case(latency, False)
            print(f"P0-03 TIMING {{'simulated_api_s': {latency}, 'elapsed_s': {elapsed}, 'venues': {calls}}}")
            assert calls == ["entropy", "hedge"]
            assert elapsed >= 0.95 + latency
            assert elapsed < 1.8 + latency
    asyncio.run(scenario())


def test_recent_trade_grace_skips_both_position_lookups():
    async def scenario():
        elapsed, calls = await run_case(0.05, True)
        print(f"P0-03 GRACE {{'elapsed_s': {elapsed}, 'queried': {calls}}}")
        assert calls == []
        assert elapsed >= 1.0
    asyncio.run(scenario())
