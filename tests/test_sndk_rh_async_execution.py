"""Synthetic causal replay checks. Run: python -m unittest tests.test_sndk_rh_async_execution"""
import unittest
from tools.analyze_sndk_rh_async_execution import evaluate,latest_at

def quote(t,venue,bid,ask):
    return dict(received_wall_ns=t,venue=venue,bids=[[bid,10]],asks=[[ask,10]],parameter_valid=True,source_timestamp_status="present")

class TestAsyncReplay(unittest.TestCase):
    def setUp(self):
        entropy=quote(90_000_000,"entropy",99,100)
        rh0=quote(95_000_000,"lighter-rh",101,102)
        rh1=quote(150_000_000,"lighter-rh",99,100)
        self.books={"entropy":([entropy],[90_000_000]),"lighter-rh":([rh0,rh1],[95_000_000,150_000_000])}
        self.signal=dict(direction="buy_entropy",signal_wall_ns=100_000_000,signal_id="s")

    def test_zero_delay_capture(self):
        x=evaluate(self.signal,self.books,0,100,"entropy",1000)
        self.assertEqual(x["status"],"complete")
        self.assertAlmostEqual(x["capture_bps"],100.)

    def test_second_leg_delay_loss(self):
        x=evaluate(self.signal,self.books,100,100,"entropy",1000)
        self.assertEqual(x["status"],"complete")
        self.assertAlmostEqual(x["capture_bps"],-100.)

    def test_second_leg_first(self):
        x=evaluate(self.signal,self.books,0,100,"lighter-rh",1000)
        self.assertEqual(x["status"],"complete")
        self.assertAlmostEqual(x["capture_bps"],100.)

    def test_no_future_quote(self):
        self.assertEqual(latest_at(*self.books["lighter-rh"],100_000_000)["bids"][0][0],101)

if __name__=="__main__":unittest.main()
