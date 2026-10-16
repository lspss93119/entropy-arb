"""P0-03 offline reproductions against unmodified upstream Engine.

Tests document current behaviors; they do not authorize a strategy change.
No credentials, network calls, or live orders.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))
from test_engine import make_engine


def test_unknown_leg_unlocks_venues_without_new_entry_gate():
    async def scenario():
        eng = make_engine()
        buy, sell = eng.hedge, eng.entropy
        # Force an already sent transaction with unknown final outcome.
        async def unresolved(*args, **kwargs):
            return True
        eng._execute = unresolved
        calls = []
        async def hedge():
            calls.append("hedge")
        eng._maybe_hedge = hedge
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy, sell, object())
        assert eng._reconcile_evt.is_set()
        assert not eng._vlock(buy.key).locked()
        assert not eng._vlock(sell.key).locked()
        assert not eng.halted
        assert calls == []
        # New book updates can permit a fresh signal before reconciliation.
        sell.set_book(100.14, 100.16)
        buy.set_book(99.99, 100.01)
        eng._scan(__import__("time").time())
        new_entry = eng._scan(__import__("time").time())
        assert new_entry is not None
    asyncio.run(scenario())


def test_execution_exception_still_triggers_hedge_path():
    async def scenario():
        eng = make_engine()
        buy, sell = eng.hedge, eng.entropy
        async def explode(*args):
            raise RuntimeError("simulated uncertain execution")
        calls = []
        async def hedge():
            calls.append("hedge")
        eng._execute = explode
        eng._maybe_hedge = hedge
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy, sell, object())
        assert calls == ["hedge"]
        assert not eng._reconcile_evt.is_set()
        assert not eng.halted
    asyncio.run(scenario())
