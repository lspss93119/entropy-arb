"""P0-04 logging regression tests; never sends real orders."""
import csv
from pathlib import Path
from types import SimpleNamespace
from test_engine import make_engine
from entropy_arb.engine import CSV_HEADER


def test_prior_trade_csv_preserved_and_new_schema_separate(tmp_path):
    eng=make_engine()
    orig=tmp_path/"trades.csv"
    orig.write_text("legacy,header\n1,2\n")
    old=tmp_path/"trades.csv.old"
    old.write_text("important older backup\n")
    eng.cfg.trades_csv=str(orig)
    plan=SimpleNamespace(qty=1,buy_limit=100,sell_limit=101,
                         buy_notional=100,sell_notional=101,
                         exp_edge_usd=1,gross_edge_usd=1,
                         marginal_premium_bps=100)
    eng._log_csv("sell_entropy",eng.hedge,eng.entropy,plan,True,1,1,
                 "filled","filled",1.0,0,execution_id="demo-001",
                 send_ts=10.0,settled_ts=10.3,buy_avg_px=100.1,
                 sell_avg_px=100.9)
    assert orig.read_text()=="legacy,header\n1,2\n"
    assert old.read_text()=="important older backup\n"
    with (tmp_path/"trades.csv.p004.csv").open() as fp:
        rows=list(csv.DictReader(fp))
    assert set(rows[0])==set(CSV_HEADER)
    assert rows[0]["execution_id"]=="demo-001"
    assert rows[0]["buy_avg_px"]=="100.1"
    assert rows[0]["sell_avg_px"]=="100.9"


def test_missing_avg_px_is_not_filled_with_limit(tmp_path):
    eng=make_engine()
    eng.cfg.trades_csv=str(tmp_path/"trades.csv")
    plan=SimpleNamespace(qty=1,buy_limit=100,sell_limit=101,
                         buy_notional=100,sell_notional=101,
                         exp_edge_usd=1,gross_edge_usd=1,
                         marginal_premium_bps=100)
    eng._log_csv("sell_entropy",eng.hedge,eng.entropy,plan,False,0,1,
                 "timeout","filled",0,0,execution_id="demo-002",
                 buy_avg_px=None,sell_avg_px=101)
    with (tmp_path/"trades.csv").open() as fp:
        row=next(csv.DictReader(fp))
    assert row["buy_avg_px"]=="" and row["sell_avg_px"]=="101"


def test_hedge_event_records_fill_and_nullable_parent(tmp_path):
    eng=make_engine()
    eng.cfg.trades_csv=str(tmp_path/"trades.csv")
    eng._log_hedge_csv("demo-001",eng.entropy,"buy",0.6,100.6,
                       {"filled_base":0.6,"avg_px":100.5,
                        "status":"filled","unresolved":False,"err":None},
                       10.5,10.7)
    eng._log_hedge_csv(None,eng.hedge,"sell",0.2,100.1,
                       {"filled_base":0,"avg_px":None,
                        "status":"timeout","unresolved":True,"err":None},
                       12.0,12.8)
    with (tmp_path/"trades.csv.hedges.csv").open() as fp:
        rows=list(csv.DictReader(fp))
    assert rows[0]["execution_id"]=="demo-001"
    assert rows[0]["filled_qty"]=="0.6"
    assert rows[0]["avg_px"]=="100.5"
    assert rows[1]["execution_id"]==""
    assert rows[1]["avg_px"]=="" and rows[1]["unresolved"]=="True"
