"""Regression: background reconcile must not initiate hedge during settlement."""
import asyncio
from test_engine import make_engine


def test_reconcile_does_not_start_second_hedge_during_settlement():
    async def scenario():
        eng = make_engine()
        eng._settlement_pending = True
        eng._unresolved_execution = False
        for venue in (eng.entropy, eng.hedge):
            venue.last_traded_ts = 0.0
        attempts = []
        async def successful_reconcile(*args):
            return True
        async def forbidden_hedge(*args, **kwargs):
            attempts.append(True)
        eng._reconcile_venue = successful_reconcile
        eng._maybe_hedge = forbidden_hedge
        await eng._reconcile_positions(hedge=True)
        assert attempts == [], "background reconcile attempted hedge during paired settlement"
    asyncio.run(scenario())
