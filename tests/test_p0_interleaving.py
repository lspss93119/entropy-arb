"""Controlled asyncio interleavings for paired execution aftermath (no network)."""
import asyncio
from pathlib import Path
from test_engine import make_engine


def test_hedge_pending_must_block_new_signal(tmp_path):
    async def scenario():
        eng=make_engine(midline=0,upper=1,lower=1)
        eng._recovery_marker=str(tmp_path/"pending.unresolved")
        buy,sell=eng.hedge,eng.entropy
        buy.set_book(99.99,100.01)
        sell.set_book(100.14,100.16)
        entered=asyncio.Event()
        release=asyncio.Event()
        async def settled(*args):
            return False
        async def delayed_hedge(**kwargs):
            entered.set()
            await release.wait()
        eng._execute=settled
        eng._maybe_hedge=delayed_hedge
        for v in (buy,sell): await eng._vlock(v.key).acquire()
        task=asyncio.create_task(eng._execute_locked(buy,sell,object()))
        await asyncio.wait_for(entered.wait(),2)
        assert Path(eng._recovery_marker).exists()
        # No new signal or entry is permitted while previous hedge is pending.
        assert eng._settlement_pending
        assert eng._scan(__import__("time").time()) is None
        release.set()
        await task
    asyncio.run(scenario())


def test_reconcile_interleave_cannot_clear_pending_marker(tmp_path):
    async def scenario():
        eng=make_engine(midline=0,upper=1,lower=1)
        eng._recovery_marker=str(tmp_path/"pending.unresolved")
        buy,sell=eng.hedge,eng.entropy
        entered=asyncio.Event()
        release=asyncio.Event()
        async def settled(*args):
            return False
        async def delayed_hedge(**kwargs):
            entered.set()
            await release.wait()
        eng._execute=settled
        eng._maybe_hedge=delayed_hedge
        for v in (buy,sell):await eng._vlock(v.key).acquire()
        task=asyncio.create_task(eng._execute_locked(buy,sell,object()))
        await asyncio.wait_for(entered.wait(),2)
        async def fake_reconcile(*args,**kwargs):
            eng._update_evt.set()
            return True
        eng._reconcile_positions=fake_reconcile
        await eng._reconcile_positions(hedge=True)
        assert Path(eng._recovery_marker).exists()
        assert eng._settlement_pending
        assert eng._scan(__import__("time").time()) is None
        release.set()
        await task
    asyncio.run(scenario())
