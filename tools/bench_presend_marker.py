#!/usr/bin/env python3
"""Measure durable pre-send marker overhead on THIS machine, without orders.

Runs multiple repetitions and uses temporary files only. Not a trading process.
"""
import argparse
import json
import os
from pathlib import Path
import statistics
import tempfile
import time


def percentile(values, pct):
    values = sorted(values)
    return round(values[int((len(values)-1)*pct)], 4)


def describe(samples):
    return {"count": len(samples), "p50_ms": percentile(samples,.5),
            "p95_ms": percentile(samples,.95), "p99_ms": percentile(samples,.99),
            "max_ms": round(max(samples),4)}


def one_cycle(folder, index):
    path=Path(folder)/"execution.unresolved"
    start=time.perf_counter_ns()
    with path.open("a", encoding="utf8") as fh:
        fh.write(f"{index} PREPARED\\n")
        fh.flush()
        os.fsync(fh.fileno())
    fd=os.open(folder,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
    ready=time.perf_counter_ns()
    path.unlink()
    fd=os.open(folder,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)
    done=time.perf_counter_ns()
    return (ready-start)/1e6,(done-ready)/1e6


def bench(iterations, directory=None):
    if iterations<10 or iterations>10000:
        raise ValueError("iterations must be 10..10000")
    if directory:
        Path(directory).mkdir(parents=True,exist_ok=True)
        ctx=tempfile.TemporaryDirectory(prefix=".p0-bench-",dir=directory)
    else:
        ctx=tempfile.TemporaryDirectory(prefix="p0-bench-")
    with ctx as folder:
        # Ignore warmup and run every experiment on the exact same filesystem.
        for i in range(10):
            one_cycle(folder,-i)
        presend,cleanup=[],[]
        for i in range(iterations):
            p,c=one_cycle(folder,i)
            presend.append(p);cleanup.append(c)
    return {"iterations":iterations,"presend_sync":describe(presend),
            "postsettle_cleanup":describe(cleanup),
            "notes":"Local filesystem benchmark only; no HTTP, WebSocket, order or signing."}


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--iterations",type=int,default=1000)
    p.add_argument("--directory",help="Existing parent directory on target filesystem")
    args=p.parse_args()
    print(json.dumps(bench(args.iterations,args.directory),indent=2))
