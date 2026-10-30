#!/usr/bin/env python3
"""Record-only Run02 asynchronous two-leg quote replay; not actual fills."""
from __future__ import annotations
import argparse, bisect, csv, json
from collections import Counter
from pathlib import Path
from tools.analyze_sndk_rh_run02_persistence import load_dataset, percentile
from tools.analyze_sndk_rh_execution_quality import walk_to_notional, _walk_to_quantity

DELAYS=(0,50,100,250,500)
TARGETS=(100.0,250.0)
DIRECTIONS={"buy_entropy":("entropy","lighter-rh"),"sell_entropy":("lighter-rh","entropy")}

def latest_at(rows, times, timestamp):
    ix=bisect.bisect_right(times,timestamp)-1
    return rows[ix] if ix>=0 else None

def check_quote(quote,t,max_age_ms):
    if quote is None: return "missing_quote"
    if quote.get("source_timestamp_status")!="present": return "missing_source_timestamp"
    if quote.get("parameter_valid") is not True: return "invalid_rolling"
    age=(t-int(quote["received_wall_ns"]))/1e6
    if age<0: return "future_quote"
    if age>max_age_ms: return "stale_quote"
    return "ok"

def evaluate(signal,books,delay_ms,target,first_venue,max_age_ms):
    direction=signal["direction"]
    buy_venue,sell_venue=DIRECTIONS[direction]
    second_venue=sell_venue if first_venue==buy_venue else buy_venue
    t0=int(signal["signal_wall_ns"]); t1=t0+int(delay_ms*1e6)
    initial={v:latest_at(*books[v],t0) for v in ("entropy","lighter-rh")}
    first=initial[first_venue]; second=latest_at(*books[second_venue],t1)
    result=dict(run_id=signal.get("_run_id"),signal_id=signal.get("signal_id"),direction=direction,
                target_usd=target,first_venue=first_venue,second_venue=second_venue,delay_ms=delay_ms,
                status="unavailable",capture_usd=None,capture_bps=None,unhedged_qty=None,
                first_quote_age_ms=None,second_quote_age_ms=None)
    for v in initial:
        reason=check_quote(initial[v],t0,max_age_ms)
        if reason!="ok":
            result["status"]="signal_"+v+"_"+reason
            return result
    if abs(int(initial["entropy"]["received_wall_ns"])-int(initial["lighter-rh"]["received_wall_ns"]))>250_000_000:
        result["status"]="signal_skew"
        return result
    for label,q,t in (("first",first,t0),("second",second,t1)):
        reason=check_quote(q,t,max_age_ms)
        if reason!="ok":
            result["status"]=label+"_"+reason
            return result
        result[label+"_quote_age_ms"]=(t-int(q["received_wall_ns"]))/1e6
    buy_cost=None; sell_revenue=None
    if first_venue==buy_venue:
        fill=walk_to_notional(first.get("asks") or [],target)
        if not fill["complete"]:
            result["status"]="first_depth_short"; return result
        qty=fill["qty"];buy_cost=fill["notional_usd"]
    else:
        basis=walk_to_notional(initial[buy_venue].get("asks") or [],target)
        if not basis["complete"]:
            result["status"]="indicative_buy_depth_short";return result
        qty=basis["qty"]
        fill=_walk_to_quantity(first.get("bids") or [],qty)
        if not fill["complete"]:
            result["status"]="first_depth_short";return result
        sell_revenue=fill["notional_usd"]
    result["first_qty"]=qty
    side="asks" if second_venue==buy_venue else "bids"
    hedge=_walk_to_quantity(second.get(side) or [],qty)
    if not hedge["complete"]:
        result["status"]="second_depth_short"
        result["unhedged_qty"]=max(0,qty-hedge["qty"])
        return result
    if second_venue==buy_venue: buy_cost=hedge["notional_usd"]
    else: sell_revenue=hedge["notional_usd"]
    result["status"]="complete"; result["unhedged_qty"]=0
    result["capture_usd"]=sell_revenue-buy_cost
    result["capture_bps"]=result["capture_usd"]/buy_cost*1e4
    return result

def analyze(data_root,output_dir,max_age_ms=1000.0):
    data=load_dataset(data_root)
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=True)
    events=[]; runs=[]
    for run in data["runs"]:
        streams={v:[] for v in ("entropy","lighter-rh")}
        for q in run["records"]["quotes"]:
            if q.get("venue") in streams: streams[q["venue"]].append(q)
        books={}
        for venue,rows in streams.items():
            rows.sort(key=lambda q:int(q["received_wall_ns"]))
            books[venue]=(rows,[int(q["received_wall_ns"]) for q in rows])
        for signal in run["records"]["signals"]:
            direction=signal.get("direction")
            if direction not in DIRECTIONS:continue
            for target in TARGETS:
                for first in DIRECTIONS[direction]:
                    for delay in DELAYS:
                        events.append(evaluate({**signal,"_run_id":run["run_id"]},books,delay,target,first,max_age_ms))
        runs.append(dict(run_id=run["run_id"],quote_count=len(run["records"]["quotes"]),signal_count=len(run["records"]["signals"])))
    fields=sorted(set().union(*(e.keys() for e in events))) if events else []
    with (output/"async_events.csv").open("w",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(events)
    summaries=[]
    for direction in DIRECTIONS:
        for target in TARGETS:
            for first in DIRECTIONS[direction]:
                for delay in DELAYS:
                    subset=[e for e in events if e["direction"]==direction and e["target_usd"]==target and e["first_venue"]==first and e["delay_ms"]==delay]
                    complete=[e for e in subset if e["status"]=="complete"]
                    n=len(subset); capture=[e["capture_bps"] for e in complete]
                    summaries.append(dict(direction=direction,target_usd=target,first_venue=first,delay_ms=delay,
                        candidates=n,complete=len(complete),complete_pct=100*len(complete)/n if n else None,
                        positive_count=sum(x>0 for x in capture),positive_pct_of_candidates=100*sum(x>0 for x in capture)/n if n else None,
                        capture_p10_bps=percentile(capture,.1),capture_p50_bps=percentile(capture,.5),
                        capture_p90_bps=percentile(capture,.9),
                        incomplete_reasons=json.dumps(dict(Counter(e["status"] for e in subset if e["status"]!="complete")),sort_keys=True)))
    with (output/"async_summary.csv").open("w",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=list(summaries[0]) if summaries else []);writer.writeheader();writer.writerows(summaries)
    (output/"async_metadata.json").write_text(json.dumps(dict(
        fee_bps=0,target_usd=TARGETS,delays_ms=DELAYS,max_receive_age_ms=max_age_ms,
        interpretation="Quote-book proxy; no actual orders, queue, partial fills, or realized PnL",
        integrity=data["integrity"],runs=runs,event_count=len(events)),indent=2,default=str))
    return summaries

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root",required=True)
    parser.add_argument("--output-dir",required=True)
    parser.add_argument("--max-age-ms",type=float,default=1000.)
    args=parser.parse_args()
    print(json.dumps(analyze(args.data_root,args.output_dir,args.max_age_ms),indent=2))

if __name__=="__main__": main()
