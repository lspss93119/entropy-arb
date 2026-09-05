"""Arcus credentials and environment configuration contract."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ArcusCreds, ConfigError, load_config  # noqa: E402


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


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


def load():
    return load_config(write_tmp(MINIMAL), NO_ENV, symbol="SNDK",
                       venue_a="arcus", venue_b="lighter-rh")


def set_arcus_env(monkeypatch):
    monkeypatch.setenv("ARCUS_ENV", "testnet")
    monkeypatch.setenv("ARCUS_API_KEY", API_KEY)
    monkeypatch.setenv("ARCUS_API_PRIVATE_KEY", PRIVATE_KEY)
    monkeypatch.setenv("ARCUS_ACCOUNT_ADDRESS", ADDRESS)
    monkeypatch.setenv("ARCUS_ACCOUNT_INDEX", "2")


def test_arcus_credentials_load_from_environment_and_select_testnet(monkeypatch):
    set_arcus_env(monkeypatch)
    cfg = load()
    creds = cfg.venue_a.arcus_creds
    assert creds is not None and creds.complete
    assert creds.api_key == API_KEY
    assert creds.account_address == ADDRESS
    assert creds.account_index == 2
    assert cfg.arcus_env == "testnet"
    assert cfg.arcus_api_url == "https://api.testnet.arcus.xyz"
    assert cfg.arcus_ws_url == "wss://api.testnet.arcus.xyz/v1/ws"


def test_incomplete_arcus_credentials_are_not_complete():
    creds = ArcusCreds(None, None, ADDRESS, 0)
    assert not creds.complete


def test_arcus_private_key_is_not_in_config_repr(monkeypatch):
    set_arcus_env(monkeypatch)
    cfg = load()
    assert PRIVATE_KEY not in repr(cfg.venue_a.arcus_creds)


def test_invalid_arcus_environment_is_rejected(monkeypatch):
    monkeypatch.setenv("ARCUS_ENV", "staging")
    with pytest.raises(ConfigError, match="ARCUS_ENV"):
        load()


def test_account_api_fee_source_is_explicit(monkeypatch):
    set_arcus_env(monkeypatch)
    cfg = load_config(write_tmp(MINIMAL.replace(
        "taker_fee_bps: 2.25", "taker_fee_bps: 2.25\n    fee_source: account_api")),
        NO_ENV, symbol="SNDK", venue_a="arcus", venue_b="lighter-rh")
    assert cfg.venue_a.arcus_fee_source == "account_api"
    assert cfg.venue_a.fee_bps == 2.25
