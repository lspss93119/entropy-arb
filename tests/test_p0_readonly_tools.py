"""Read-only probe smoke and deterministic offline metric checks."""
import asyncio
import importlib.util
from pathlib import Path
from entropy_arb.book import OrderBook

def load(name):
    s=importlib.util.spec_from_file_location(name,Path(__file__).resolve().parents[1]/"tools"/(name+".py"))
    m=importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m

def test_marker_benchmark_has_no_network_and_reports_percentiles():
    m=load("bench_presend_marker")
    report=m.bench(10)
    assert report["presend_sync"]["count"]==10
    assert 0 <= report["presend_sync"]["p50_ms"] <= report["presend_sync"]["max_ms"]

def test_book_summary_handles_missing_events():
    m=load("probe_public_book_live")
    assert m.summary([])=={"count":0}
    assert m.summary([1,2,3])["max"]==3

def test_book_observation_works_with_fake_feed_only():
    m=load("probe_public_book_live")
    async def run():
        book=OrderBook()
        class FakeFeed:
            notify=lambda self:None
            async def run(self,stop):
                book.apply_hl([[{"px":"100","sz":"1"}],[{"px":"101","sz":"1"}]],source_ts=None)
                self.notify()
                await stop.wait()
        report=await m.observe(FakeFeed(),book,.01,0)
        assert report["counts"]["updates"]==1
        assert report["counts"]["missing_source_ts"]==1
    asyncio.run(run())
