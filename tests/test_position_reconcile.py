"""Regression tests for legacy position reconciliation behavior."""
import asyncio
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from entropy_arb.venue_hl import HLVenue  # noqa: E402


NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def _config():
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write("""
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
""")
    f.close()
    return load_config(f.name, NO_ENV,
                       symbol="SNDK", hedge_venue="lighter-rh")


def test_hl_position_read_missing_coin_returns_zero_legacy():
    venue = object.__new__(HLVenue)
    venue.conf = SimpleNamespace(hl_dex="io")
    venue.coin = "io:SNDK"
    venue._query_address = lambda: "0x" + "1" * 40

    async def info(_payload):
        return {
            "assetPositions": [{
                "position": {"coin": "io:OAI", "szi": "1.0"},
                "type": "oneWay",
            }],
            "marginSummary": {"totalNtlPos": "100.0"},
            "time": 1789059989000,
        }

    venue._info = info
    assert asyncio.run(venue.fetch_position()) == 0.0


def test_hl_position_read_malformed_asset_positions_returns_zero_legacy():
    venue = object.__new__(HLVenue)
    venue.conf = SimpleNamespace(hl_dex="io")
    venue.coin = "io:SNDK"
    venue._query_address = lambda: "0x" + "1" * 40

    async def info(_payload):
        return {"assetPositions": [{}], "time": 1789059989000}

    venue._info = info
    assert asyncio.run(venue.fetch_position()) == 0.0


class _PositionVenue:
    def __init__(self, key, name, position, read):
        self.key = key
        self.name = name
        self.position = position
        self.cash = 0.0
        self.book = OrderBook()
        self.last_traded_ts = 0.0
        self.read = read

    async def fetch_position(self):
        return self.read


def test_reconcile_adopts_zero_for_missing_hl_target_legacy():
    eng = Engine(_config())
    entropy = _PositionVenue("entropy", "ENTROPY", 0.0294, 0.0)
    hedge = _PositionVenue("hedge", "RH", 0.0, 0.0)
    eng.entropy = entropy
    eng.hedge = hedge
    eng.venues = {"entropy": entropy, "hedge": hedge}
    called = []

    async def maybe_hedge():
        called.append(True)

    eng._maybe_hedge = maybe_hedge
    asyncio.run(eng._reconcile_positions(hedge=True))

    assert entropy.position == 0.0
    assert called == [True]
