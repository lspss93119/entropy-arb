"""P0 cross-component integration guards, all mocked and offline."""
import asyncio
import csv
import time
from types import SimpleNamespace

from test_engine import make_engine
from entropy_arb.engine import Engine


def test_p001_cap_is_respected_with_p002_freshness():
    async def check():
        eng=make_engine(midline=0,upper=1,lower=1)
        eng.cfg.max_order_notional=100.0
        eng.entropy.set_book(100.14,100.16)
        eng.hedge.set_book(99.99,100.01)
        first=eng._scan(time.time())
        best=eng._scan(time.time())
        assert best is not None
        buy,sell,plan=best
        assert plan.buy_notional <= 100.000001
        assert plan.sell_notional <= 100.000001
        # A stale/expired market should veto the whole operation, irrespective of cap.
        eng.entropy.book.alive_ts=0
        assert eng._scan(time.time()) is None

    asyncio.run(check())

def test_p003_unknown_gate_with_p002_live_book_and_p004_logging(tmp_path):
    async def scenario():
        eng=make_engine(midline=0,upper=1,lower=1)
        eng.cfg.trades_csv=str(tmp_path/"trades.csv")
        eng._recovery_marker=str(tmp_path/"pending.unresolved")
        buy,sell=eng.hedge,eng.entropy
        buy.set_book(99.99,100.01)
        sell.set_book(100.14,100.16)
        plan=SimpleNamespace(qty=1,buy_limit=100.01,sell_limit=100.14,
                             buy_notional=100.01,sell_notional=100.14,
                             exp_edge_usd=.13,gross_edge_usd=.13,
                             marginal_premium_bps=13,q_max_notional=100.01,
                             buy_fee=0,sell_fee=0)
        # Both real venue order calls are stubbed: one filled, the other unresolved.
        for venue in (buy,sell):
            venue.px_round=lambda p,round_up=False: p
            venue.volume_usd=0
        async def unknown(**kwargs):
            return dict(status="timeout",filled_base=0.0,avg_px=None,
                        unresolved=True,err=None)
        async def filled(**kwargs):
            return dict(status="filled",filled_base=1.0,avg_px=100.14,
                        unresolved=False,err=None)
        buy.send_taker=unknown
        sell.send_taker=filled
        eng._log_hedge_csv=lambda *args: (_ for _ in ()).throw(
            AssertionError("unknown order must not trigger hedge"))
        await eng._vlock(buy.key).acquire()
        await eng._vlock(sell.key).acquire()
        await eng._execute_locked(buy,sell,plan)
        assert eng._unresolved_execution
        assert (tmp_path/"pending.unresolved").exists()
        assert eng._reconcile_evt.is_set()
        # A new favorable book signal cannot open another execution.
        buy.set_book(99.99,100.01)
        sell.set_book(100.14,100.16)
        assert eng._scan(time.time()) is None
        with (tmp_path/"trades.csv").open() as fp:
            row=next(csv.DictReader(fp))
        assert row["execution_id"]
        assert row["buy_avg_px"]=="" and row["sell_avg_px"]=="100.14"
        assert row["buy_status"]=="timeout"
    asyncio.run(scenario())
