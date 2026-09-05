"""Offline Arcus account-read contract tests."""
import asyncio
import json
import os
import sys
import tempfile
from urllib.parse import urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import load_config  # noqa: E402


ROOT = os.path.join(os.path.dirname(__file__), "..")
FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
PRIVATE_KEY = (
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
API_KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
ADDRESS = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"

MINIMAL = """
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
venues:
  arcus:
    taker_fee_bps: 2.25
"""


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, headers=None, **kwargs):
        path = urlparse(url).path
        self.calls.append({"path": path, "params": params, "headers": headers})
        return FakeResponse(self.routes[path])


def make_venue(monkeypatch, routes, yaml_text=MINIMAL):
    monkeypatch.setenv("ARCUS_ENV", "testnet")
    monkeypatch.setenv("ARCUS_API_KEY", API_KEY)
    monkeypatch.setenv("ARCUS_API_PRIVATE_KEY", PRIVATE_KEY)
    monkeypatch.setenv("ARCUS_ACCOUNT_ADDRESS", ADDRESS)
    monkeypatch.setenv("ARCUS_ACCOUNT_INDEX", "2")
    cfg = load_config(write_tmp(yaml_text), NO_ENV, symbol="SNDK",
                      venue_a="arcus", venue_b="lighter-rh")
    from entropy_arb.venue_arcus import ArcusVenue
    venue = ArcusVenue(cfg.venue_a, cfg.arcus_api_url, cfg.arcus_ws_url,
                       FakeSession(routes), 5.0, environment=cfg.arcus_env)
    venue.market_id = 33
    venue.market_display_name = "SNDK-USD"
    return venue


def test_account_reads_normalize_equity_position_orders_fills_fee_and_rate(
        monkeypatch):
    routes = {
        "/v1/account": fixture("arcus_account.json"),
        "/v1/positions": fixture("arcus_positions.json"),
        "/v1/openOrders": fixture("arcus_open_orders.json"),
        "/v1/fills": fixture("arcus_fills.json"),
        "/v1/account/stats": fixture("arcus_account_stats.json"),
        "/v1/rateLimit": fixture("arcus_rate_limit.json"),
    }
    venue = make_venue(monkeypatch, routes)
    assert asyncio.run(venue.fetch_equity()) == (1240.0, 1000.0)
    assert asyncio.run(venue.fetch_position()) == -0.25
    assert asyncio.run(venue.fetch_open_orders()) == routes["/v1/openOrders"]["orders"]
    assert asyncio.run(venue.fetch_fills()) == routes["/v1/fills"]["fills"]
    fee = asyncio.run(venue.fetch_fee_tier())
    assert fee == {"level": 0, "maker_fee_bps": 1.5,
                   "taker_fee_bps": 4.5, "source": "account_api"}
    assert asyncio.run(venue.fetch_rate_limit_usage()) == routes["/v1/rateLimit"]

    session = venue.session
    assert all(call["params"]["address"] == ADDRESS for call in session.calls)
    assert all(call["params"]["accountIndex"] == "2"
               for call in session.calls if call["path"] != "/v1/account/stats")
    assert all(call["headers"] == {"X-API-Key": API_KEY}
               for call in session.calls)


@pytest.mark.parametrize("side, size, expected", [
    ("LONG", "0.25", 0.25),
    ("SHORT", "0.25", -0.25),
    ("SHORT", "-0.25", -0.25),
    ("FLAT", "0", 0.0),
])
def test_position_side_normalization(side, size, expected):
    from entropy_arb.venue_arcus import normalize_position
    assert normalize_position({"side": side, "size": size}) == expected


def test_account_reads_require_complete_arcus_credentials(monkeypatch):
    monkeypatch.delenv("ARCUS_API_KEY", raising=False)
    monkeypatch.delenv("ARCUS_API_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("ARCUS_ACCOUNT_ADDRESS", raising=False)
    monkeypatch.delenv("ARCUS_ACCOUNT_INDEX", raising=False)
    cfg = load_config(write_tmp(MINIMAL), NO_ENV, symbol="SNDK",
                      venue_a="arcus", venue_b="lighter-rh")
    from entropy_arb.venue_arcus import ArcusVenue
    venue = ArcusVenue(cfg.venue_a, cfg.arcus_api_url, cfg.arcus_ws_url,
                       FakeSession({}), 5.0)
    venue.market_id = 33
    with pytest.raises(RuntimeError, match="credentials"):
        asyncio.run(venue.fetch_position())
