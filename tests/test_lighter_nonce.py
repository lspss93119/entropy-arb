"""Regression tests for the Lighter/RH transaction nonce path."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lighter  # noqa: E402
from lighter import nonce_manager as sdk_nonce_manager  # noqa: E402

from entropy_arb.venue_lighter import LighterVenue  # noqa: E402


def _venue() -> LighterVenue:
    conf = SimpleNamespace(
        key="hedge",
        label="RH",
        symbol="ANTH",
        fee_bps=0.0,
        cap_usd=1_000.0,
        orders_per_min=60,
        lighter_profile=SimpleNamespace(
            api_url="https://api.rh.lighter.xyz",
            ws_url="wss://api.rh.lighter.xyz/stream",
            chain_id=466324,
            name="lighter-rh",
        ),
        lighter_creds=SimpleNamespace(
            account_index=202,
            api_key_index=8,
            api_private_key="test-only",
            complete=True,
        ),
    )
    venue = LighterVenue(conf, None, settle_timeout_sec=0.1)
    venue.market_id = 17
    venue.price_decimals = 2
    venue.size_decimals = 3
    return venue


class _ServerNonceManager:
    """A small API-manager double with a race if callers are not serialized."""

    def __init__(self, first_nonce=100):
        self.next_value = first_nonce
        self.calls = []

    async def async_next_nonce(self, api_key_index):
        self.calls.append(api_key_index)
        nonce = self.next_value
        await asyncio.sleep(0)
        self.next_value += 1
        return api_key_index, nonce


class _RecordingSigner:
    ORDER_TYPE_MARKET = 1
    ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 0
    DEFAULT_IOC_EXPIRY = 0

    def __init__(self, nonce_manager, responses=None):
        self.nonce_manager = nonce_manager
        self.calls = []
        self.responses = list(responses or [])

    async def create_order(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0) if self.responses else None
        if response is not None:
            return response
        return object(), SimpleNamespace(code=200), None


def test_rh_signer_uses_api_nonce_manager_and_chain(monkeypatch):
    captured = {}

    class FakeSigner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def check_client(self):
            return None

    monkeypatch.setattr(lighter, "SignerClient", FakeSigner)

    venue = _venue()
    venue.init_signer()

    assert captured["chain_id"] == 466324
    assert (captured["nonce_management_type"]
            is sdk_nonce_manager.NonceManagerType.API)


def test_sequential_primary_and_residual_share_authoritative_nonce_path():
    venue = _venue()
    manager = _ServerNonceManager(first_nonce=100)
    signer = _RecordingSigner(manager)
    venue.signer = signer

    async def run():
        primary = await venue.send_taker(
            is_buy=False, qty=0.007, limit_px=2_000.0)
        residual = await venue.send_taker(
            is_buy=True, qty=0.007, limit_px=2_001.0, reduce_only=True)
        return primary, residual

    primary, residual = asyncio.run(run())

    assert primary["status"] == residual["status"] == "sent-unconfirmed"
    assert [call["nonce"] for call in signer.calls] == [100, 101]
    assert [call["api_key_index"] for call in signer.calls] == [8, 8]
    assert manager.calls == [8, 8]


def test_concurrent_signed_orders_cannot_reuse_nonce():
    venue = _venue()
    manager = _ServerNonceManager(first_nonce=200)
    signer = _RecordingSigner(manager)
    venue.signer = signer

    async def run():
        return await asyncio.gather(*(
            venue.send_taker(is_buy=False, qty=0.007, limit_px=2_000.0)
            for _ in range(4)
        ))

    results = asyncio.run(run())

    assert all(result["status"] == "sent-unconfirmed" for result in results)
    assert [call["nonce"] for call in signer.calls] == [200, 201, 202, 203]


def test_failed_send_is_not_retried_and_next_send_refreshes_nonce():
    venue = _venue()
    manager = _ServerNonceManager(first_nonce=300)
    signer = _RecordingSigner(manager, responses=[
        (None, None, "HTTP response body: code=21104 message='invalid nonce'"),
    ])
    venue.signer = signer

    async def run():
        failed = await venue.send_taker(
            is_buy=False, qty=0.007, limit_px=2_000.0)
        accepted = await venue.send_taker(
            is_buy=False, qty=0.007, limit_px=2_000.0)
        return failed, accepted

    failed, accepted = asyncio.run(run())

    assert failed["status"] == "send-failed"
    assert "invalid nonce" in failed["err"]
    assert accepted["status"] == "sent-unconfirmed"
    assert len(signer.calls) == 2
    assert [call["nonce"] for call in signer.calls] == [300, 301]


def test_invalid_nonce_remains_a_surfaced_execution_failure():
    venue = _venue()
    manager = _ServerNonceManager(first_nonce=400)
    signer = _RecordingSigner(manager, responses=[
        (None, None, "HTTP response body: code=21104 message='invalid nonce'"),
    ])
    venue.signer = signer

    result = asyncio.run(venue.send_taker(
        is_buy=False, qty=0.007, limit_px=2_000.0))

    assert result["status"] == "send-failed"
    assert result["unresolved"] is False
    assert result["err"].startswith("HTTP response body: code=21104")
    assert len(signer.calls) == 1


def test_rh_credentials_keep_existing_account_and_api_key_mapping():
    venue = _venue()
    creds = venue.conf.lighter_creds

    assert creds.account_index == 202
    assert creds.api_key_index == 8
    assert creds.api_private_key == "test-only"
    assert venue.profile.chain_id == 466324


if __name__ == "__main__":
    pytest.main([__file__])
