"""Config loading: example file, validation, CLI-selected markets.

Run:  python3 -m pytest tests/  (or  python3 tests/test_config.py)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import ConfigError, load_config  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
EXAMPLE = os.path.join(ROOT, "config.example.yaml")
NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")


def write_tmp(text: str) -> str:
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    f.write(text)
    f.close()
    return f.name


MINIMAL = """
thresholds:
  midline_bps: 5.0
  upper_bps: 4.0
  lower_bps: 3.0
"""


def load(yaml_text: str, symbol="SNDK", hedge="lighter-rh"):
    return load_config(write_tmp(yaml_text), NO_ENV,
                       symbol=symbol, hedge_venue=hedge)


def test_example_config_loads():
    cfg = load_config(EXAMPLE, NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    assert cfg.symbol == "SNDK"
    assert cfg.entropy.kind == "hl" and cfg.entropy.hl_dex == "io"
    assert cfg.hedge_venue == "lighter-rh"
    assert cfg.hedge.kind == "lighter"
    assert cfg.hedge.lighter_profile.chain_id == 466324
    assert cfg.entropy.symbol == "SNDK" and cfg.hedge.symbol == "SNDK"
    assert cfg.recorder_enabled and cfg.recorder_csv
    assert cfg.dashboard and cfg.log_file


def test_minimal_defaults():
    cfg = load(MINIMAL, hedge="lighter")
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0
    assert cfg.hedge.label == "LIGHTER"
    assert cfg.hedge.lighter_profile.chain_id == 304
    assert cfg.take_fraction == 0.5          # defaults kick in
    assert cfg.recorder_enabled is True
    assert cfg.recorder_csv == "logs/record/minutes.csv"
    assert cfg.trades_csv == "logs/trades/trades.csv"
    assert cfg.log_file == "logs/engine/engine.log"


def test_tradexyz_hedge():
    cfg = load(MINIMAL, hedge="tradexyz")
    assert cfg.hedge.kind == "hl" and cfg.hedge.hl_dex == "xyz"
    assert cfg.hedge.label == "XYZ"


def expect_error(yaml_text: str, needle: str, **kw):
    try:
        load(yaml_text, **kw)
    except ConfigError as e:
        assert needle in str(e), f"{needle!r} not in {e}"
        return
    raise AssertionError(f"expected ConfigError containing {needle!r}")


def test_unknown_key_rejected():
    expect_error(MINIMAL + "\nthresholdz:\n  x: 1\n",
                 "unknown config key 'thresholdz'")
    expect_error(MINIMAL + "\nsizing:\n  take_fractionn: 0.5\n",
                 "sizing.take_fractionn")


def test_markets_no_longer_config_keys():
    # symbol / hedge_venue moved to --symbol / --hedge: leftovers in the
    # YAML must fail loudly, not silently override the flags
    expect_error("symbol: SNDK\n" + MINIMAL, "unknown config key 'symbol'")
    expect_error("hedge_venue: tradexyz\n" + MINIMAL,
                 "unknown config key 'hedge_venue'")


def test_bad_cli_markets():
    expect_error(MINIMAL, "--hedge", hedge="binance")
    expect_error(MINIMAL, "--symbol", symbol="")


def test_missing_thresholds():
    expect_error("recorder:\n  enabled: true\n", "thresholds.")


def test_nonpositive_band():
    expect_error("thresholds:\n"
                 "  midline_bps: 5\n  upper_bps: 0\n  lower_bps: 3\n",
                 "must be > 0")


def test_lighter_credentials_are_selected_by_deployment(monkeypatch):
    for name in (
        "LIGHTER_MAINNET_ACCOUNT_INDEX",
        "LIGHTER_MAINNET_API_KEY_INDEX",
        "LIGHTER_MAINNET_API_PRIVATE_KEY",
        "LIGHTER_RH_ACCOUNT_INDEX",
        "LIGHTER_RH_API_KEY_INDEX",
        "LIGHTER_RH_API_PRIVATE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("LIGHTER_MAINNET_ACCOUNT_INDEX", "101")
    monkeypatch.setenv("LIGHTER_MAINNET_API_KEY_INDEX", "7")
    monkeypatch.setenv("LIGHTER_MAINNET_API_PRIVATE_KEY", "mainnet-test-key")
    monkeypatch.setenv("LIGHTER_RH_ACCOUNT_INDEX", "202")
    monkeypatch.setenv("LIGHTER_RH_API_KEY_INDEX", "8")
    monkeypatch.setenv("LIGHTER_RH_API_PRIVATE_KEY", "rh-test-key")

    mainnet = load(MINIMAL, hedge="lighter")
    robinhood = load(MINIMAL, hedge="lighter-rh")

    main_creds = mainnet.hedge.lighter_creds
    rh_creds = robinhood.hedge.lighter_creds
    assert (main_creds.account_index, main_creds.api_key_index,
            main_creds.api_private_key) == (101, 7, "mainnet-test-key")
    assert (rh_creds.account_index, rh_creds.api_key_index,
            rh_creds.api_private_key) == (202, 8, "rh-test-key")


def test_lighter_credentials_never_fall_back_across_deployments(monkeypatch):
    for name in (
        "LIGHTER_MAINNET_ACCOUNT_INDEX",
        "LIGHTER_MAINNET_API_KEY_INDEX",
        "LIGHTER_MAINNET_API_PRIVATE_KEY",
        "LIGHTER_RH_ACCOUNT_INDEX",
        "LIGHTER_RH_API_KEY_INDEX",
        "LIGHTER_RH_API_PRIVATE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("LIGHTER_MAINNET_ACCOUNT_INDEX", "101")
    monkeypatch.setenv("LIGHTER_MAINNET_API_KEY_INDEX", "7")
    monkeypatch.setenv("LIGHTER_MAINNET_API_PRIVATE_KEY", "mainnet-test-key")

    robinhood = load(MINIMAL, hedge="lighter-rh")
    creds = robinhood.hedge.lighter_creds
    assert creds.account_index is None
    assert creds.api_key_index is None
    assert creds.api_private_key is None
    assert not creds.complete


def test_one_selected_lighter_namespace_does_not_require_the_other(monkeypatch):
    for name in (
        "LIGHTER_MAINNET_ACCOUNT_INDEX",
        "LIGHTER_MAINNET_API_KEY_INDEX",
        "LIGHTER_MAINNET_API_PRIVATE_KEY",
        "LIGHTER_RH_ACCOUNT_INDEX",
        "LIGHTER_RH_API_KEY_INDEX",
        "LIGHTER_RH_API_PRIVATE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("LIGHTER_RH_ACCOUNT_INDEX", "202")
    monkeypatch.setenv("LIGHTER_RH_API_KEY_INDEX", "8")
    monkeypatch.setenv("LIGHTER_RH_API_PRIVATE_KEY", "rh-test-key")

    cfg = load(MINIMAL, hedge="lighter-rh")
    assert cfg.hedge.lighter_creds.complete
    assert cfg.hedge.lighter_creds.account_index == 202


def test_lighter_symbol_aliases_are_scoped_to_each_deployment():
    for hedge in ("lighter", "lighter-rh"):
        oai = load(MINIMAL, symbol="OAI", hedge=hedge)
        anth = load(MINIMAL, symbol="ANTH", hedge=hedge)

        assert oai.symbol == "OAI"
        assert oai.entropy.symbol == "OAI"
        assert oai.hedge.symbol == "OPENAI"
        assert anth.symbol == "ANTH"
        assert anth.entropy.symbol == "ANTH"
        assert anth.hedge.symbol == "ANTHROPIC"


def test_lighter_symbols_without_aliases_keep_the_canonical_name():
    for symbol in ("SNDK", "NBIS", "GPRO"):
        cfg = load(MINIMAL, symbol=symbol, hedge="lighter-rh")
        assert cfg.symbol == symbol
        assert cfg.entropy.symbol == symbol
        assert cfg.hedge.symbol == symbol


def test_native_symbol_input_normalizes_to_canonical_symbol():
    for native, canonical in (("OPENAI", "OAI"),
                              ("ANTHROPIC", "ANTH")):
        cfg = load(MINIMAL, symbol=f"  {native.lower()}  ",
                   hedge="lighter-rh")
        assert cfg.symbol == canonical
        assert cfg.entropy.symbol == canonical
        assert cfg.hedge.symbol == native


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
