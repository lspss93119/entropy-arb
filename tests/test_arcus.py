"""Arcus public market-data adapter tests using sanitized production fixtures."""
import asyncio
import copy
import csv
import importlib.util
import json
import os
import sys
import tempfile
import time
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook  # noqa: E402
from entropy_arb.config import ConfigError, load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402
from entropy_arb.recorder import HEADER, MinuteRecorder  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
MARKETS_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                               "arcus_markets_sndk.json")
BBO_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                           "arcus_bbo_messages.json")


MINIMAL = """
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
venues:
  arcus:
    taker_fee_bps: 2.25
    max_position_usd: 1000
    max_orders_per_min: 30
"""


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


def load(yaml_text: str = MINIMAL, *, venue_a="arcus", venue_b="lighter-rh"):
    return load_config(write_tmp(yaml_text), NO_ENV, symbol="SNDK",
                       venue_a=venue_a, venue_b=venue_b)


def arcus_module():
    spec = importlib.util.find_spec("entropy_arb.venue_arcus")
    assert spec is not None, "Arcus read-only adapter module is required"
    return __import__("entropy_arb.venue_arcus", fromlist=["ArcusVenue"])


def fixture(name):
    with open(name) as fh:
        return json.load(fh)


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
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append((url, params))
        return FakeResponse(self.payload)


def test_arcus_is_supported_and_preserves_actual_identity():
    cfg = load()
    assert cfg.venue_a.venue_name == "arcus"
    assert cfg.venue_a.kind == "arcus"
    assert cfg.venue_a.key == "venue_a"
    assert cfg.venue_a.fee_bps == 2.25
    assert cfg.venue_a.cap_usd == 1000.0
    assert cfg.venue_a.orders_per_min == 30


def test_arcus_requires_explicit_fee_configuration():
    with pytest.raises(ConfigError, match="arcus.*taker_fee_bps.*explicit"):
        load("""
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
""")


def test_arcus_can_be_venue_b_in_non_entropy_pair():
    cfg = load(venue_a="lighter-rh", venue_b="arcus")
    assert cfg.venue_a.venue_name == "lighter-rh"
    assert cfg.venue_b.venue_name == "arcus"
    assert cfg.venue_b.key == "venue_b"


def test_cli_lists_arcus_as_a_supported_venue():
    result = __import__("subprocess").run(
        [sys.executable, os.path.join(ROOT, "main.py"), "--help"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "arcus" in result.stdout


def test_engine_rejects_arcus_live_execution_before_constructing_venues(monkeypatch):
    cfg = load()
    engine = Engine(cfg, record_only=False)

    def unexpected_make_venue(_):
        raise AssertionError("live Arcus must be rejected before venue creation")

    monkeypatch.setattr(engine, "_make_venue", unexpected_make_venue)
    with pytest.raises(RuntimeError, match="Arcus live execution not implemented"):
        asyncio.run(engine._run_inner())


def test_arcus_market_mapping_loads_market_id_and_precision():
    module = arcus_module()
    cfg = load()
    venue = module.ArcusVenue(cfg.venue_a, "https://api.arcus.xyz",
                              "wss://api.arcus.xyz/v1/ws",
                              FakeSession(fixture(MARKETS_FIXTURE)), 5.0)
    asyncio.run(venue.load_market())
    assert venue.market_display_name == "SNDK-USD"
    assert venue.market_id == 33
    assert venue.tick_size == Decimal("0.01")
    assert venue.step_size == Decimal("0.0000001")
    assert venue.size_decimals == 7
    assert venue.min_base == 0.01
    assert venue.min_quote == 5.0


@pytest.mark.parametrize("mutation, message", [
    (lambda rows: rows.clear(), "not found"),
    (lambda rows: rows.append(copy.deepcopy(rows[0])), "ambiguous"),
    (lambda rows: rows[0].update(type="SPOT"), "not perpetual"),
    (lambda rows: rows[0].update(status="OFFLINE"), "not online"),
])
def test_arcus_market_mapping_rejects_invalid_market_selection(mutation, message):
    module = arcus_module()
    payload = fixture(MARKETS_FIXTURE)
    rows = payload["markets"]
    if message == "ambiguous":
        duplicate = copy.deepcopy(rows[0])
        rows.append(duplicate)
    else:
        mutation(rows)
    cfg = load()
    venue = module.ArcusVenue(cfg.venue_a, "https://api.arcus.xyz",
                              "wss://api.arcus.xyz/v1/ws",
                              FakeSession(payload), 5.0)
    with pytest.raises(RuntimeError, match=message):
        asyncio.run(venue.load_market())


def test_arcus_bbo_fixture_populates_real_top_of_book():
    module = arcus_module()
    book = OrderBook()
    notifications = []
    feed = module.ArcusBBOFeed("ARCUS", "wss://example.invalid/v1/ws",
                               "SNDK-USD", book, lambda: notifications.append(1))
    for message in fixture(BBO_FIXTURE):
        feed.handle_message(message)
    assert book.ready
    assert book.best_bid() == 1762.74
    assert book.best_ask() == 1763.58
    assert book.bids[1762.74] == 1.6819938
    assert book.asks[1763.58] == 0.4256124
    assert feed.last_sequence_id == 93594320
    assert feed.last_global_sequence_id == 1856155977
    assert len(notifications) == 2


@pytest.mark.parametrize("field", ["bestBid", "bestAsk"])
def test_arcus_bbo_missing_side_leaves_book_not_ready(field):
    module = arcus_module()
    book = OrderBook()
    feed = module.ArcusBBOFeed("ARCUS", "wss://example.invalid/v1/ws",
                               "SNDK-USD", book, lambda: None)
    feed.handle_message(fixture(BBO_FIXTURE)[1])
    message = copy.deepcopy(fixture(BBO_FIXTURE)[1])
    message["contents"][field] = None
    feed.handle_message(message)
    assert not book.ready
    assert book.best_bid() is None
    assert book.best_ask() is None


def test_arcus_bbo_malformed_numeric_value_is_rejected():
    module = arcus_module()
    book = OrderBook()
    feed = module.ArcusBBOFeed("ARCUS", "wss://example.invalid/v1/ws",
                               "SNDK-USD", book, lambda: None)
    message = copy.deepcopy(fixture(BBO_FIXTURE)[1])
    message["contents"]["bestBid"]["price"] = "not-a-number"
    with pytest.raises(ValueError, match="BBO"):
        feed.handle_message(message)


def test_arcus_bbo_freshness_expires_without_a_real_update():
    module = arcus_module()
    book = OrderBook()
    feed = module.ArcusBBOFeed("ARCUS", "wss://example.invalid/v1/ws",
                               "SNDK-USD", book, lambda: None)
    feed.handle_message(fixture(BBO_FIXTURE)[1])
    book.alive_ts = time.time() - 11
    assert not book.is_fresh(10.0)


def test_arcus_bbo_ignores_out_of_order_exchange_update():
    module = arcus_module()
    book = OrderBook()
    feed = module.ArcusBBOFeed("ARCUS", "wss://example.invalid/v1/ws",
                               "SNDK-USD", book, lambda: None)
    current = fixture(BBO_FIXTURE)[1]
    feed.handle_message(current)
    stale = copy.deepcopy(current)
    stale["contents"]["lastSequenceId"] -= 1
    stale["contents"]["timestamp"] -= 1
    stale["contents"]["bestBid"]["price"] = "1"
    stale["contents"]["bestAsk"]["price"] = "2"
    feed.handle_message(stale)
    assert book.best_bid() == 1762.92
    assert book.best_ask() == 1763.37


def test_arcus_feed_uses_only_the_verified_bbo_subscription(monkeypatch):
    from entropy_arb import feeds

    sent = []
    stop = asyncio.Event()

    class FakeWebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def send(self, raw):
            sent.append(json.loads(raw))

        def __aiter__(self):
            self.messages = iter([
                {"type": "connected"},
                fixture(BBO_FIXTURE)[1],
            ])
            return self

        async def __anext__(self):
            try:
                message = next(self.messages)
            except StopIteration:
                stop.set()
                raise StopAsyncIteration
            return json.dumps(message)

    monkeypatch.setattr(feeds, "ws_connect",
                        lambda *args, **kwargs: FakeWebSocket())
    feed = feeds.ArcusBBOFeed("ARCUS", "wss://example.invalid/v1/ws",
                              "SNDK-USD", OrderBook(), lambda: None)
    asyncio.run(feed.run(stop))
    assert sent == [{"type": "subscribe", "channel": "bbo", "id": "SNDK-USD"}]


def test_arcus_venue_has_no_live_execution_contract():
    module = arcus_module()
    cfg = load()
    venue = module.ArcusVenue(cfg.venue_a, "https://api.arcus.xyz",
                              "wss://api.arcus.xyz/v1/ws",
                              FakeSession(fixture(MARKETS_FIXTURE)), 5.0)
    assert not hasattr(venue, "send_taker")
    with pytest.raises(RuntimeError, match="Arcus live execution not implemented"):
        venue.start_tasks(asyncio.Event(), lambda: None, live=True)


def test_arcus_bbo_can_be_recorded_with_generic_metadata(tmp_path):
    module = arcus_module()
    arcus_book = OrderBook()
    feed = module.ArcusBBOFeed("ARCUS", "wss://example.invalid/v1/ws",
                               "SNDK-USD", arcus_book, lambda: None)
    feed.handle_message(fixture(BBO_FIXTURE)[1])
    entropy_book = OrderBook()
    entropy_book.apply_hl([[{"px": "1763.00", "sz": "1"}],
                           [{"px": "1763.10", "sz": "1"}]])
    path = str(tmp_path / "minutes-SNDK-entropy-arcus.csv")
    recorder = MinuteRecorder(path, entropy_book, arcus_book, 1e9,
                              venue_a_name="entropy", venue_b_name="arcus",
                              symbol="SNDK")
    recorder.sample(1_700_000_000.0)
    recorder.close()
    with open(path, newline="") as fh:
        row = next(csv.DictReader(fh))
    assert list(row) == HEADER
    assert row["venue_a"] == "entropy"
    assert row["venue_b"] == "arcus"
    assert row["symbol"] == "SNDK"
    assert float(row["b_bid"]) == 1762.92
    assert float(row["b_ask"]) == 1763.37


class LifecycleVenue:
    def __init__(self, conf):
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.kind = conf.kind
        self.venue_name = conf.venue_name
        self.book = OrderBook()
        self.position = self.cash = self.volume_usd = 0.0
        self.equity = self.free = self.start_equity = None
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.size_decimals = 4
        self.min_base = 0.0001
        self.min_quote = 10.0
        self.loaded = False
        self.started = False
        self.closed = False

    async def load_market(self):
        self.loaded = True

    def start_tasks(self, stop, notify, live):
        assert not live
        self.started = True
        return []

    def ready_to_trade(self):
        return self.book.ready

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("venue_a, venue_b", [("entropy", "arcus"),
                                               ("lighter-rh", "arcus")])
def test_record_only_pair_lifecycle_supports_arcus_without_live_methods(
        monkeypatch, tmp_path, venue_a, venue_b):
    yaml_text = MINIMAL + "\n" if venue_a == "arcus" else """
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
venues:
  arcus:
    taker_fee_bps: 2.25
"""
    cfg = load(yaml_text, venue_a=venue_a, venue_b=venue_b)
    created = {}
    engine = Engine(cfg, record_only=True)

    def make_venue(conf):
        created[conf.key] = LifecycleVenue(conf)
        return created[conf.key]

    monkeypatch.setattr(engine, "_make_venue", make_venue)
    monkeypatch.chdir(tmp_path)
    engine.stop.set()
    asyncio.run(engine._run_inner())
    assert set(created) == {"venue_a", "venue_b"}
    assert all(venue.loaded and venue.started and venue.closed
               for venue in created.values())
    assert created["venue_a"].venue_name == venue_a
    assert created["venue_b"].venue_name == venue_b
