"""P0 pre-send durable marker controls; mocked orders only."""
import asyncio
from types import SimpleNamespace
from pathlib import Path
from test_engine import make_engine


def _setup(tmp_path):
    eng=make_engine()
    eng._recovery_marker=str(tmp_path/"order.unresolved")
    buy,sell=eng.hedge,eng.entropy
    return eng,buy,sell


def test_prepared_before_send_and_normal_completion_clears(tmp_path):
    async def run():
        eng,buy,sell=_setup(tmp_path)
        observed=[]
        async def success(*args):
            observed.append(Path(eng._recovery_marker).exists())
            return False
        eng._execute=success
        eng._maybe_hedge=lambda **kwargs: asyncio.sleep(0)
        for v in (buy,sell):
            await eng._vlock(v.key).acquire()
        await eng._execute_locked(buy,sell,object())
        assert observed == [True]
        assert not Path(eng._recovery_marker).exists()
        assert not eng._unresolved_execution
    asyncio.run(run())


def test_failed_marker_prevents_any_send(tmp_path):
    async def run():
        eng,buy,sell=_setup(tmp_path)
        called=[]
        def fail(*args):
            raise OSError("write unavailable")
        eng._persist_unknown=fail
        async def should_not_send(*args):
            called.append(True)
        eng._execute=should_not_send
        for v in (buy,sell):
            await eng._vlock(v.key).acquire()
        await eng._execute_locked(buy,sell,object())
        assert called==[]
        assert eng.halted and eng._unresolved_execution
    asyncio.run(run())


def test_unknown_result_keeps_restart_marker(tmp_path):
    async def run():
        eng,buy,sell=_setup(tmp_path)
        async def unknown(*args):
            assert Path(eng._recovery_marker).exists()
            return True
        eng._execute=unknown
        for v in (buy,sell):
            await eng._vlock(v.key).acquire()
        await eng._execute_locked(buy,sell,object())
        assert Path(eng._recovery_marker).exists()
        assert eng._unresolved_execution
    asyncio.run(run())


def test_unbalanced_or_failed_hedge_preserves_marker(tmp_path):
    async def run():
        eng,buy,sell=_setup(tmp_path)
        async def success(*args):return False
        eng._execute=success
        buy.position=1.0
        sell.position=0.0
        async def incomplete(**kwargs): return None
        eng._maybe_hedge=incomplete
        for v in (buy,sell): await eng._vlock(v.key).acquire()
        await eng._execute_locked(buy,sell,object())
        assert Path(eng._recovery_marker).exists()
        assert eng._unresolved_execution is True
    asyncio.run(run())
