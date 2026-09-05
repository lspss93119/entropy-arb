"""Config loading: generic pair selection and venue-specific settings.

Run:  python3 -m pytest tests/  (or  python3 tests/test_config.py)
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb import config as config_module  # noqa: E402
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


def load(yaml_text: str, symbol="SNDK", venue_a="entropy",
         venue_b="lighter-rh"):
    return load_config(write_tmp(yaml_text), NO_ENV,
                       symbol=symbol, venue_a=venue_a, venue_b=venue_b)


def test_example_config_loads():
    cfg = load_config(EXAMPLE, NO_ENV,
                      symbol="SNDK", venue_a="entropy", venue_b="lighter-rh")
    assert cfg.symbol == "SNDK"
    assert cfg.venue_a.key == "venue_a"
    assert cfg.venue_a.venue_name == "entropy"
    assert cfg.venue_a.kind == "hl" and cfg.venue_a.hl_dex == "io"
    assert cfg.venue_b.key == "venue_b"
    assert cfg.venue_b.venue_name == "lighter-rh"
    assert cfg.venue_b.kind == "lighter"
    assert cfg.venue_b.lighter_profile.chain_id == 466324
    assert cfg.venue_a.symbol == "SNDK" and cfg.venue_b.symbol == "SNDK"
    assert cfg.recorder_enabled and cfg.recorder_csv
    assert cfg.recorder_csv == "logs/minutes-SNDK-entropy-lighter-rh.csv"
    assert cfg.dashboard and cfg.log_file


def test_minimal_defaults():
    cfg = load(MINIMAL, venue_b="lighter")
    assert cfg.midline_bps == 5.0 and cfg.upper_bps == 4.0 and cfg.lower_bps == 3.0
    assert cfg.venue_a.label == "ENTROPY"
    assert cfg.venue_b.label == "LIGHTER"
    assert cfg.venue_b.lighter_profile.chain_id == 304
    assert cfg.take_fraction == 0.5          # defaults kick in
    assert cfg.recorder_enabled is True
    assert cfg.recorder_csv == "logs/minutes-SNDK-entropy-lighter.csv"


def test_non_entropy_pair_builds_from_actual_venue_names():
    cfg = load(MINIMAL, venue_a="lighter-rh", venue_b="tradexyz")
    assert cfg.venue_a.key == "venue_a"
    assert cfg.venue_a.venue_name == "lighter-rh"
    assert cfg.venue_a.kind == "lighter"
    assert cfg.venue_a.lighter_profile.chain_id == 466324
    assert cfg.venue_b.key == "venue_b"
    assert cfg.venue_b.venue_name == "tradexyz"
    assert cfg.venue_b.kind == "hl" and cfg.venue_b.hl_dex == "xyz"
    assert cfg.recorder_csv == "logs/minutes-SNDK-lighter-rh-tradexyz.csv"


def test_venue_settings_follow_identity_not_role():
    cfg = load(MINIMAL + """
venues:
  lighter-rh:
    taker_fee_bps: 2.5
    max_position_usd: 1234
    max_orders_per_min: 17
  tradexyz:
    taker_fee_bps: 3.5
    max_position_usd: 2345
    max_orders_per_min: 19
""", venue_a="lighter-rh", venue_b="tradexyz")
    assert cfg.venue_a.fee_bps == 2.5
    assert cfg.venue_a.cap_usd == 1234.0
    assert cfg.venue_a.orders_per_min == 17
    assert cfg.venue_b.fee_bps == 3.5
    assert cfg.venue_b.cap_usd == 2345.0
    assert cfg.venue_b.orders_per_min == 19


def test_explicit_recorder_path_is_preserved():
    cfg = load(MINIMAL + """
recorder:
  csv: custom/minutes.csv
""")
    assert cfg.recorder_csv == "custom/minutes.csv"


def test_venue_builder_is_explicit_and_role_based():
    builder = getattr(config_module, "build_venue_conf", None)
    assert builder is not None, "generic venue builder is required"
    raw = {"venues": {"lighter-rh": {"taker_fee_bps": 2.0}}}
    vc = builder("lighter-rh", "venue_a", "SNDK", raw)
    assert vc.key == "venue_a"
    assert vc.venue_name == "lighter-rh"
    assert vc.kind == "lighter"
    assert vc.fee_bps == 2.0


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


def test_old_role_config_keys_are_rejected():
    expect_error(MINIMAL + "\nentropy:\n  dex: io\n",
                 "unknown config key 'entropy'")
    expect_error(MINIMAL + "\nhedge:\n  taker_fee_bps: 0\n",
                 "unknown config key 'hedge'")


def test_markets_are_cli_only():
    # symbol / venue selection must not be silently overridden by YAML.
    expect_error("symbol: SNDK\n" + MINIMAL, "unknown config key 'symbol'")
    expect_error("hedge_venue: tradexyz\n" + MINIMAL,
                 "unknown config key 'hedge_venue'")


def test_equal_venues_rejected():
    expect_error(MINIMAL, "must be different", venue_a="lighter-rh",
                 venue_b="lighter-rh")


def test_unknown_venue_rejected():
    expect_error(MINIMAL, "supported venues", venue_a="binance")
    expect_error(MINIMAL, "supported venues", venue_b="binance")


def test_bad_symbol_rejected():
    expect_error(MINIMAL, "--symbol", symbol="")


def test_cli_exposes_generic_pair_flags():
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "main.py"), "--help"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "--venue-a" in result.stdout
    assert "--venue-b" in result.stdout
    assert "--hedge" not in result.stdout


def test_cli_rejects_unknown_venue_before_startup():
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "main.py"),
         "--record-only", "--symbol", "SNDK", "--venue-a", "binance",
         "--venue-b", "lighter-rh"],
        capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_cli_rejects_equal_venues():
    result = subprocess.run(
        [sys.executable, os.path.join(ROOT, "main.py"),
         "--record-only", "--symbol", "SNDK", "--venue-a", "lighter-rh",
         "--venue-b", "lighter-rh", "--config", write_tmp(MINIMAL),
         "--env-file", NO_ENV],
        capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "must be different" in result.stderr


def test_missing_thresholds():
    expect_error("recorder:\n  enabled: true\n", "thresholds.")


def test_nonpositive_band():
    expect_error("thresholds:\n"
                 "  midline_bps: 5\n  upper_bps: 0\n  lower_bps: 3\n",
                 "must be > 0")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
