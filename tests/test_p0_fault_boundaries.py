"""Cross-P0 fault probes: record actual limitations without live network."""
import asyncio
import csv
import time
from types import SimpleNamespace
from test_engine import make_engine


def test_csv_write_failure_does_not_change_execution_decision(tmp_path, monkeypatch):
    async def scenario():
        eng=make_engine(midline=0, upper=1, lower=1)
        eng.cfg.trades_csv=str(tmp_path/"trades.csv")
        eng.hedge.set_book(99.99,100.01)
        eng.entropy.set_book(100.14,100.16)
        buy,sell=eng.hedge,eng.entropy
        for venue in (buy,sell):
            venue.px_round=lambda p,round_up=False:p
            venue.volume_usd=0.0
        async def filled_buy(**kwargs):
            return {"status":"filled","filled_base":1.0,"avg_px":100.01,
                    "err":None,"unresolved":False}
        async def filled_sell(**kwargs):
            return {"status":"filled","filled_base":1.0,"avg_px":100.14,
                    "err":None,"unresolved":False}
        buy.send_taker=filled_buy
        sell.send_taker=filled_sell
        # Unknown output directory? Simulate disk/permissions failure in logger.
        def broken_open(*args, **kwargs):
            raise OSError("disk unavailable")
        monkeypatch.setattr("builtins.open",broken_open)
        plan=SimpleNamespace(qty=1,buy_limit=100.01,sell_limit=100.14,
                             buy_notional=100.01,sell_notional=100.14,
                             exp_edge_usd=.13,gross_edge_usd=.13,
                             marginal_premium_bps=13,q_max_notional=100.01,
                             buy_fee=0,sell_fee=0)
        unresolved=await eng._execute(buy,sell,plan)
        assert unresolved is False
        assert eng.total_fill_edge>0
    asyncio.run(scenario())


def test_marker_is_created_before_execution_settles(tmp_path):
    """Pre-send durability: marker exists while order awaits its response."""
    async def scenario():
        eng=make_engine()
        eng._recovery_marker=str(tmp_path/"incident.unresolved")
        buy,sell=eng.hedge,eng.entropy
        entered=asyncio.Event()
        release=asyncio.Event()
        async def unknown_execution(*args):
            entered.set()
            await release.wait()
            return True
        eng._execute=unknown_execution
        for venue in (buy,sell):
            await eng._vlock(venue.key).acquire()
        task=asyncio.create_task(eng._execute_locked(buy,sell,object()))
        await entered.wait()
        assert (tmp_path/"incident.unresolved").exists()
        release.set()
        await task
        assert (tmp_path/"incident.unresolved").exists()
    asyncio.run(scenario())
