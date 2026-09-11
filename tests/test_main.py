"""CLI helpers for multi-market record-only collection."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402

_market_output_path = main._market_output_path
_record_pairs = main._record_pairs
_split_cli_values = main._split_cli_values


def test_split_cli_values_and_record_pairs_are_unique():
    assert _split_cli_values(["SNDK,BTC", "SNDK"]) == ["SNDK", "BTC"]
    assert _record_pairs(["SNDK", "BTC"], ["lighter", "lighter-rh"]
                         ) == [
                             ("SNDK", "lighter"),
                             ("SNDK", "lighter-rh"),
                             ("BTC", "lighter"),
                             ("BTC", "lighter-rh"),
                         ]


def test_market_output_path_is_namespaced_by_pair():
    assert _market_output_path("logs/record/minutes.csv", "SNDK", "lighter-rh") \
        == "logs/record/minutes-SNDK-lighter-rh.csv"
    assert _market_output_path("logs/trades/trades.csv", "SNDK", "lighter-rh") \
        == "logs/trades/trades-SNDK-lighter-rh.csv"
    assert _market_output_path("logs/engine/engine.log", "SNDK", "lighter-rh") \
        == "logs/engine/engine-SNDK-lighter-rh.log"


def test_all_runtime_outputs_are_namespaced_for_each_pair():
    cfg = SimpleNamespace(
        symbol="SNDK",
        hedge_venue="lighter-rh",
        recorder_csv="logs/record/minutes.csv",
        trades_csv="logs/trades/trades.csv",
        log_file="logs/engine/engine.log",
    )
    namespace = getattr(main, "_namespace_config_outputs", None)
    assert callable(namespace)
    assert namespace([cfg]) == [cfg]
    assert cfg.recorder_csv == "logs/record/minutes-SNDK-lighter-rh.csv"
    assert cfg.trades_csv == "logs/trades/trades-SNDK-lighter-rh.csv"
    assert cfg.log_file == "logs/engine/engine-SNDK-lighter-rh.log"
