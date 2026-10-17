"""Cancellation and failure lifecycle regression; no live network/orders."""
import asyncio
from pathlib import Path
from test_engine import make_engine

def setup(tmp_path):
    eng=make_engine()
    eng._recovery_marker=str(tmp_path/"pending.unresolved")
    return eng,eng.hedge,eng.entropy

def test_cancel_while_order_pending_retains_marker_and_gate(tmp_path):
    async def run():
        eng,buy,sell=setup(tmp_path)
        started=asyncio.Event()
        async def never(*args):
            started.set()
            await asyncio.Future()
        eng._execute=never
        for v in (buy,sell):await eng._vlock(v.key).acquire()
        task=asyncio.create_task(eng._execute_locked(buy,sell,object()))
        await started.wait()
        task.cancel()
        try:await task
        except asyncio.CancelledError:pass
        assert Path(eng._recovery_marker).exists()
        assert eng._unresolved_execution or eng._settlement_pending or eng.halted
    asyncio.run(run())

def test_cancel_during_hedge_retains_marker_and_gate(tmp_path):
    async def run():
        eng,buy,sell=setup(tmp_path)
        started=asyncio.Event()
        async def settled(*args):return False
        async def never(**kwargs):
            started.set()
            await asyncio.Future()
        eng._execute=settled
        eng._maybe_hedge=never
        for v in (buy,sell):await eng._vlock(v.key).acquire()
        task=asyncio.create_task(eng._execute_locked(buy,sell,object()))
        await started.wait()
        task.cancel()
        try:await task
        except asyncio.CancelledError:pass
        assert Path(eng._recovery_marker).exists()
        assert eng._unresolved_execution or eng._settlement_pending or eng.halted
    asyncio.run(run())

def test_execute_exception_retains_marker_and_gate(tmp_path):
    async def run():
        eng,buy,sell=setup(tmp_path)
        async def broken(*args):
            raise RuntimeError("unknown order outcome")
        eng._execute=broken
        for v in (buy,sell):await eng._vlock(v.key).acquire()
        await eng._execute_locked(buy,sell,object())
        assert Path(eng._recovery_marker).exists()
        assert eng._unresolved_execution
    asyncio.run(run())
