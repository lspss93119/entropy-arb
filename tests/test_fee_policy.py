"""TDD contract tests for normalized effective taker fees."""
import asyncio
import os
import sys
import tempfile
from urllib.parse import urlparse

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402
from entropy_arb.venue_lighter import LighterVenue  # noqa: E402
from entropy_arb.venue_arcus import ArcusVenue  # noqa: E402


NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
PRIVATE_KEY = (
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
API_KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
ADDRESS = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


def arcus_yaml(fee_source: str) -> str:
    return f"""
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
venues:
  arcus:
    taker_fee_bps: 2.25
    fee_source: {fee_source}
"""


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
        self.calls.append({"path": path, "params": params,
                           "headers": headers})
        route = self.routes[path]
        if isinstance(route, tuple):
            payload, status = route
        else:
            payload, status = route, 200
        return FakeResponse(payload, status)


def make_arcus(monkeypatch, source="account_api", routes=None, credentials=True):
    if credentials:
        monkeypatch.setenv("ARCUS_ENV", "testnet")
        monkeypatch.setenv("ARCUS_API_KEY", API_KEY)
        monkeypatch.setenv("ARCUS_API_PRIVATE_KEY", PRIVATE_KEY)
        monkeypatch.setenv("ARCUS_ACCOUNT_ADDRESS", ADDRESS)
        monkeypatch.setenv("ARCUS_ACCOUNT_INDEX", "0")
    else:
        for name in ("ARCUS_ENV", "ARCUS_API_KEY", "ARCUS_API_PRIVATE_KEY",
                     "ARCUS_ACCOUNT_ADDRESS", "ARCUS_ACCOUNT_INDEX"):
            monkeypatch.delenv(name, raising=False)

    cfg = load_config(write_tmp(arcus_yaml(source)), NO_ENV,
                      symbol="SNDK", venue_a="arcus", venue_b="lighter-rh")
    session = FakeSession(routes or {})
    venue = ArcusVenue(cfg.venue_a, cfg.arcus_api_url, cfg.arcus_ws_url,
                       session, 5.0, environment=cfg.arcus_env)
    venue.market_id = 33
    return venue, session


def test_static_venue_exposes_configured_effective_fee():
    cfg = load_config(write_tmp("""
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
venues:
  lighter-rh:
    taker_fee_bps: 2.0
"""), NO_ENV, symbol="SNDK", venue_a="lighter-rh",
                      venue_b="tradexyz")
    venue = LighterVenue(cfg.venue_a, FakeSession({}), 5.0)
    assert getattr(venue, "fee_source", None) == "configured"
    assert getattr(venue, "effective_taker_fee_bps", None) == 2.0


def test_arcus_account_api_fee_replaces_stale_configured_fee(monkeypatch):
    venue, _ = make_arcus(monkeypatch, routes={
        "/v1/account/stats": {
            "tradingFeeTier": {
                "level": 0, "makerFeePpm": 150, "takerFeePpm": 450,
            }
        }
    })
    resolver = getattr(venue, "resolve_effective_fee", None)
    assert callable(resolver)
    assert asyncio.run(resolver(live=True)) == 4.5
    assert venue.effective_taker_fee_bps == 4.5
    assert venue.fee_source == "account_api"


def test_account_api_fee_unavailable_fails_closed_without_yaml_fallback(
        monkeypatch):
    venue, _ = make_arcus(monkeypatch, routes={
        "/v1/account/stats": ({"error": "temporary unavailable"}, 503),
    })
    venue.book.apply_hl([[{"px": "100", "sz": "1"}],
                         [{"px": "101", "sz": "1"}]])
    resolver = getattr(venue, "resolve_effective_fee", None)
    assert callable(resolver)
    with pytest.raises(RuntimeError, match="effective fee unavailable"):
        asyncio.run(resolver(live=True))
    assert venue.effective_taker_fee_bps is None
    assert not venue.ready_to_trade()


def test_account_api_source_is_not_required_for_record_only(monkeypatch):
    venue, session = make_arcus(monkeypatch, credentials=False)
    resolver = getattr(venue, "resolve_effective_fee", None)
    assert callable(resolver)
    assert asyncio.run(resolver(live=False)) is None
    assert venue.effective_taker_fee_bps is None
    assert session.calls == []


def test_fee_source_is_rejected_for_static_venues():
    with pytest.raises(ConfigError, match="fee_source"):
        load_config(write_tmp("""
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
venues:
  lighter-rh:
    taker_fee_bps: 2.0
    fee_source: account_api
"""), NO_ENV, symbol="SNDK", venue_a="lighter-rh",
                    venue_b="tradexyz")
