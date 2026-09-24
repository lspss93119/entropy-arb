"""Run-level strategy parameter logging."""
import asyncio
import csv
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine, RUN_CONFIG_HEADER  # noqa: E402


NO_ENV = "/tmp/entropy-arb-no-such.env"


def test_run_config_snapshot_records_effective_strategy_parameters(tmp_path,
                                                                  monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
thresholds:
  midline_bps: -4.4
  upper_bps: 3.5
  lower_bps: 5.0
execution:
  premium_persist_sec: 0.5
  cooldown_sec: 1.0
  leg_slippage_bps: 10.0
  hedge_slippage_bps: 15.0
sizing:
  take_fraction: 0.25
  max_order_notional_usd: 50
inventory:
  scale_bps: 10.0
""")
    cfg = load_config(str(config_file), NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    cfg.log_file = str(tmp_path / "logs" / "engine.log")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    monkeypatch.setenv("ENTROPY_ARB_CODE_VERSION", "a1fbb32")

    eng = Engine(cfg, record_only=False)
    eng._write_run_config()

    path = tmp_path / "logs" / "runs-SNDK-lighter-rh.csv"
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == RUN_CONFIG_HEADER
        row = next(reader)

    assert row["run_id"] == eng.run_id
    assert row["mode"] == "live"
    assert row["strategy_mode"] == "fixed"
    assert row["symbol"] == "SNDK"
    assert row["hedge"] == "lighter-rh"
    assert float(row["cooldown_sec"]) == 1.0
    assert float(row["midline_bps"]) == -4.4
    assert float(row["upper_bps"]) == 3.5
    assert float(row["lower_bps"]) == 5.0
    assert float(row["premium_persist_sec"]) == 0.5
    assert float(row["leg_slippage_bps"]) == 10.0
    assert float(row["hedge_slippage_bps"]) == 15.0
    assert float(row["take_fraction"]) == 0.25
    assert float(row["max_order_notional_usd"]) == 50.0
    assert float(row["inventory_scale_bps"]) == 10.0
    assert row["host_region"] == "ap-northeast-1"
    assert row["market_data_mode"] == (
        "entropy:hl_l2book_fast|hedge:lighter_order_book")
    assert row["entropy_order_transport"] == "http_exchange"
    assert row["hedge_order_transport"] == "lighter_sdk"
    assert row["code_version"] == "a1fbb32"


def test_run_config_snapshot_records_rolling_parameters(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
strategy:
  mode: rolling
rolling:
  window_hours: 12
  update_minutes: 15
  min_coverage_pct: 80
  seed_from_csv: true
  min_exit_capture_bps: 0.0
""")
    cfg = load_config(str(config_file), NO_ENV,
                      symbol="ANTH", hedge_venue="lighter-rh")
    cfg.log_file = str(tmp_path / "logs" / "engine.log")

    eng = Engine(cfg, record_only=False)
    eng._write_run_config()

    path = tmp_path / "logs" / "runs-ANTH-lighter-rh.csv"
    with open(path, newline="") as fh:
        row = next(csv.DictReader(fh))

    assert row["strategy_mode"] == "rolling"
    assert float(row["rolling_window_hours"]) == 12.0
    assert int(row["rolling_update_minutes"]) == 15
    assert float(row["rolling_min_coverage_pct"]) == 80.0
    assert row["rolling_seed_from_csv"] == "1"
    assert float(row["rolling_min_exit_capture_bps"]) == 0.0


def test_engine_run_writes_run_config_before_runtime(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("""
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
""")
    cfg = load_config(str(config_file), NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    cfg.log_file = str(tmp_path / "logs" / "engine.log")
    eng = Engine(cfg, record_only=True)

    async def no_runtime():
        return None

    eng._run_inner = no_runtime
    asyncio.run(eng.run())

    assert not (tmp_path / "logs" / "runs-SNDK-lighter-rh.csv").exists()
