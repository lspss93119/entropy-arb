"""CLI helpers for multi-market record-only collection."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402


def test_split_cli_values_and_record_pairs_are_unique():
    assert main._split_cli_values(["SNDK,BTC", "SNDK"]) == ["SNDK", "BTC"]
    assert main._record_pairs(["SNDK", "BTC"], ["lighter", "lighter-rh"]
                              ) == [
                                  ("SNDK", "lighter"),
                                  ("SNDK", "lighter-rh"),
                                  ("BTC", "lighter"),
                                  ("BTC", "lighter-rh"),
                              ]


def test_market_output_path_is_namespaced_by_pair():
    assert main._market_output_path(
        "logs/record/minutes.csv", "SNDK", "lighter-rh"
    ) == "logs/record/minutes-SNDK-lighter-rh.csv"
    assert main._market_output_path(
        "logs/trades/trades.csv", "SNDK", "lighter-rh"
    ) == "logs/trades/trades-SNDK-lighter-rh.csv"
    assert main._market_output_path(
        "logs/engine/engine.log", "SNDK", "lighter-rh"
    ) == "logs/engine/engine-SNDK-lighter-rh.log"


def test_all_runtime_outputs_are_namespaced_for_each_pair():
    cfg = SimpleNamespace(
        symbol="SNDK",
        hedge_venue="lighter-rh",
        recorder_csv="logs/record/minutes.csv",
        trades_csv="logs/trades/trades.csv",
        log_file="logs/engine/engine.log",
    )
    assert main._namespace_config_outputs([cfg]) == [cfg]
    assert cfg.recorder_csv == "logs/record/minutes-SNDK-lighter-rh.csv"
    assert cfg.trades_csv == "logs/trades/trades-SNDK-lighter-rh.csv"
    assert cfg.log_file == "logs/engine/engine-SNDK-lighter-rh.log"


def test_multiple_pairs_are_rejected_outside_record_only(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", [
        "main.py", "--symbol", "SNDK", "--symbol", "BTC",
        "--hedge", "lighter",
    ])
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
    assert "only with --record-only" in capsys.readouterr().err


def test_record_only_runner_constructs_credential_free_engines(monkeypatch):
    created = []

    class FakeEngine:
        def __init__(self, cfg, record_only=False):
            created.append((cfg, record_only))

        def request_stop(self):
            pass

        async def run(self):
            return None

    monkeypatch.setattr(main, "Engine", FakeEngine)
    asyncio.run(main.amain_record_only(["pair-a", "pair-b"]))
    assert created == [("pair-a", True), ("pair-b", True)]
