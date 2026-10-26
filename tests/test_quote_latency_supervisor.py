"""Bounded supervisor tests for the read-only quote-latency collector."""
from __future__ import annotations

from tools.quote_latency_supervisor import build_capture_command


def test_capture_command_is_one_hour_public_sndk_rh_only():
    config = {
        "python_path": "/opt/venv/bin/python",
        "cli_path": "/opt/app/tools/record_sndk_rh_quote_latency.py",
        "output_dir": "/data/runs",
        "rolling_csv": "/data/minutes-SNDK-lighter-rh.csv",
        "fees_bps": 0,
        "sell_quantile": 0.8,
        "buy_quantile": 0.5,
        "retention_hours": 72,
        "max_file_rows": 50000,
        "max_file_bytes": 8388608,
        "max_files": 2,
        "depth_levels": 5,
        "pair_max_skew_ms": 250,
        "preserve_raw": True,
    }

    command = build_capture_command(config)

    assert command[:2] == ["/opt/venv/bin/python", "/opt/app/tools/record_sndk_rh_quote_latency.py"]
    assert "--symbol" in command and command[command.index("--symbol") + 1] == "SNDK"
    assert "--hedge" in command and command[command.index("--hedge") + 1] == "lighter-rh"
    assert "--duration-sec" in command and command[command.index("--duration-sec") + 1] == "3600"
    assert "--rolling-csv" in command
    assert "--retention-hours" in command
    assert "--preserve-raw" in command
