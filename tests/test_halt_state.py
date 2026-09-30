"""Persisted safety halt and explicit resume regression tests."""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main as cli_main  # noqa: E402
import entropy_arb.engine as engine_module  # noqa: E402
from entropy_arb.config import load_config  # noqa: E402
from entropy_arb.engine import Engine  # noqa: E402


NO_ENV = os.path.join("/tmp", "entropy-arb-no-such.env")


OPERATOR_CONFIG = """
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
  seed_from_csv: false
  min_exit_capture_bps: 0
execution:
  premium_persist_sec: 0.0
"""


def make_cfg(tmp_path, mode="rolling"):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        """
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
  seed_from_csv: false
  min_exit_capture_bps: 0
execution:
  premium_persist_sec: 0.0
"""
        if mode == "rolling" else
        """
thresholds:
  midline_bps: 0.0
  upper_bps: 4.0
  lower_bps: 4.0
"""
    )
    cfg = load_config(str(config_file), NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    cfg.trades_csv = str(tmp_path / "logs" / "trades.csv")
    cfg.recorder_csv = str(tmp_path / "logs" / "minutes.csv")
    cfg.log_file = str(tmp_path / "logs" / "engine.log")
    return cfg


def halt_path(engine):
    return engine._halt_state_path()


def write_operator_config(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(OPERATOR_CONFIG)
    return config_file


def operator_cfg(tmp_path, config_file=None):
    config_file = config_file or write_operator_config(tmp_path)
    cfg = load_config(str(config_file), NO_ENV,
                      symbol="SNDK", hedge_venue="lighter-rh")
    cli_main._namespace_config_outputs([cfg])
    return cfg


def run_operator_halt(monkeypatch, tmp_path, *extra):
    config_file = write_operator_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "main.py", "--config", str(config_file), "--env-file", NO_ENV,
        "--symbol", "SNDK", "--hedge", "lighter-rh", *extra, "--halt",
    ])
    cli_main.main()
    return tmp_path / "logs" / "state" / "halt-SNDK-lighter-rh.json"


def test_operator_halt_creates_persisted_state_without_starting_engine(
        monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli_main, "amain",
        lambda *args, **kwargs: pytest.fail("--halt started the engine"),
    )

    path = run_operator_halt(monkeypatch, tmp_path)

    with path.open() as fh:
        payload = json.load(fh)
    assert payload["halted"] is True
    assert payload["reason"] == "operator/manual halt"
    assert payload["source"] == "operator"
    assert payload["timestamp"]


def test_operator_halt_sends_no_orders_or_strategy_work(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(cli_main, "amain", lambda *args, **kwargs: called.append(1))

    run_operator_halt(monkeypatch, tmp_path)

    assert called == []


def test_startup_after_operator_halt_remains_halted(tmp_path, monkeypatch):
    path = run_operator_halt(monkeypatch, tmp_path)
    cfg = operator_cfg(tmp_path)
    eng = Engine(cfg)

    assert path.exists()
    assert eng.halted is True
    eng._scan = lambda _now: pytest.fail("operator HALT allowed strategy scan")
    asyncio.run(eng._evaluate())
    assert eng.trades == 0


def test_operator_halt_is_idempotent_and_restart_stays_halted(
        monkeypatch, tmp_path):
    path = run_operator_halt(monkeypatch, tmp_path)
    with path.open() as fh:
        first = json.load(fh)

    run_operator_halt(monkeypatch, tmp_path)
    with path.open() as fh:
        second = json.load(fh)

    assert second["halted"] is True
    assert second["reason"] == "operator/manual halt"
    assert second["source"] == "operator"
    assert second["timestamp"] >= first["timestamp"]
    assert Engine(operator_cfg(tmp_path)).halted is True


def test_operator_halt_repairs_malformed_state_to_fail_closed_halt(
        monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    cfg = operator_cfg(tmp_path)
    path = halt_path(Engine(cfg))
    path = os.fspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write("not-json")

    assert Engine(cfg).halted is True
    run_operator_halt(monkeypatch, tmp_path)

    with open(path) as fh:
        payload = json.load(fh)
    assert payload["halted"] is True
    assert Engine(operator_cfg(tmp_path)).halted is True


def test_operator_halt_write_failure_exits_nonzero(monkeypatch, tmp_path,
                                                   capsys):
    def fail_write(_path, _payload):
        raise OSError("disk full")

    monkeypatch.setattr(Engine, "_atomic_json_write", staticmethod(fail_write))

    with pytest.raises(SystemExit) as exc:
        run_operator_halt(monkeypatch, tmp_path)

    assert exc.value.code == 1
    assert "halt error" in capsys.readouterr().err


def test_operator_halt_readback_mismatch_exits_nonzero(monkeypatch, tmp_path,
                                                       capsys):
    original_load = engine_module.json.load

    def mismatched_load(fh):
        payload = original_load(fh)
        payload["halted"] = False
        payload["resume_timestamp"] = payload["timestamp"]
        return payload

    monkeypatch.setattr(engine_module.json, "load", mismatched_load)

    with pytest.raises(SystemExit) as exc:
        run_operator_halt(monkeypatch, tmp_path)

    assert exc.value.code == 1
    assert "halt error" in capsys.readouterr().err


def test_operator_halt_rejects_resume_and_record_only_combinations(
        monkeypatch, tmp_path):
    config_file = write_operator_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    for extra in (("--resume",), ("--record-only",)):
        monkeypatch.setattr(sys, "argv", [
            "main.py", "--config", str(config_file), "--env-file", NO_ENV,
            "--symbol", "SNDK", "--hedge", "lighter-rh", *extra, "--halt",
        ])
        with pytest.raises(SystemExit) as exc:
            cli_main.main()
        assert exc.value.code == 2


def test_halt_persists_across_new_engine_instance(tmp_path):
    cfg = make_cfg(tmp_path)
    first = Engine(cfg)

    first._halt_rolling("position mismatch")

    second = Engine(cfg)
    assert second.halted is True
    assert second._halt_reason == "position mismatch"
    assert second._halt_timestamp


def test_restart_alone_does_not_resume(tmp_path):
    cfg = make_cfg(tmp_path)
    first = Engine(cfg)
    first._halt_rolling("operator review required")

    restarted = Engine(cfg)

    assert restarted.halted is True
    with open(halt_path(restarted)) as fh:
        assert json.load(fh)["halted"] is True


def test_halted_evaluation_does_not_scan_or_trade(tmp_path):
    cfg = make_cfg(tmp_path)
    eng = Engine(cfg)
    eng._halt_rolling("safety stop")
    eng._scan = lambda _now: pytest.fail("halted engine scanned strategy")

    asyncio.run(eng._evaluate())

    assert eng.trades == 0


def test_explicit_resume_passes_and_persists_false(tmp_path):
    cfg = make_cfg(tmp_path)
    Engine.write_operator_halt(cfg)

    resumed = Engine(cfg)
    resumed.entropy = type("Venue", (), {"key": "entropy", "position": 0.0,
                                         "name": "ENTROPY"})()
    resumed.hedge = type("Venue", (), {"key": "hedge", "position": 0.0,
                                       "name": "RH"})()
    resumed.venues = {"entropy": resumed.entropy, "hedge": resumed.hedge}
    resumed._check_rolling_start_state()

    resumed.resume_after_reconcile()

    assert resumed.halted is False
    with open(halt_path(resumed)) as fh:
        payload = json.load(fh)
    assert payload["halted"] is False
    assert payload["resume_timestamp"]


def test_resume_rejected_on_position_mismatch(tmp_path):
    cfg = make_cfg(tmp_path)
    Engine.write_operator_halt(cfg)

    resumed = Engine(cfg)
    resumed.entropy = type("Venue", (), {"key": "entropy", "position": 0.1,
                                         "name": "ENTROPY"})()
    resumed.hedge = type("Venue", (), {"key": "hedge", "position": 0.0,
                                       "name": "RH"})()
    resumed.venues = {"entropy": resumed.entropy, "hedge": resumed.hedge}
    resumed._rolling_ledger_loaded = True

    with pytest.raises(RuntimeError, match="position|reconciliation"):
        resumed.resume_after_reconcile()

    assert resumed.halted is True
    with open(halt_path(resumed)) as fh:
        assert json.load(fh)["halted"] is True


def test_resume_rejected_with_unresolved_execution(tmp_path):
    cfg = make_cfg(tmp_path)
    Engine.write_operator_halt(cfg)

    resumed = Engine(cfg)
    resumed.entropy = type("Venue", (), {"key": "entropy", "position": 0.0,
                                         "name": "ENTROPY"})()
    resumed.hedge = type("Venue", (), {"key": "hedge", "position": 0.0,
                                       "name": "RH"})()
    resumed.venues = {"entropy": resumed.entropy, "hedge": resumed.hedge}
    resumed._check_rolling_start_state()
    resumed._exec_tasks.add(object())

    with pytest.raises(RuntimeError, match="execution|in-flight"):
        resumed.resume_after_reconcile()

    assert resumed.halted is True


def test_second_halt_updates_reason_and_timestamp(tmp_path):
    cfg = make_cfg(tmp_path)
    eng = Engine(cfg)

    eng._halt_rolling("first reason")
    with open(halt_path(eng)) as fh:
        first = json.load(fh)
    eng._halt_rolling("second reason")
    with open(halt_path(eng)) as fh:
        second = json.load(fh)

    assert first["reason"] == "first reason"
    assert second["reason"] == "second reason"
    assert second["timestamp"] != first["timestamp"]


def test_malformed_halt_state_is_fail_safe(tmp_path):
    cfg = make_cfg(tmp_path)
    eng = Engine(cfg)
    os.makedirs(os.path.dirname(halt_path(eng)), exist_ok=True)
    with open(halt_path(eng), "w") as fh:
        fh.write("not-json")

    restarted = Engine(cfg)

    assert restarted.halted is True
    assert restarted._halt_state_error
    restarted._scan = lambda _now: pytest.fail("malformed state traded")
    asyncio.run(restarted._evaluate())
    with pytest.raises(RuntimeError, match="halt"):
        restarted.resume_after_reconcile()


def test_persisted_halt_reconcile_is_observation_only(tmp_path):
    eng = Engine(make_cfg(tmp_path))
    eng._halt_rolling("operator review required")
    observed = []

    async def reconcile(*, hedge, strict=False):
        observed.append((hedge, strict))
        eng.stop.set()

    eng._reconcile_positions = reconcile
    asyncio.run(eng._reconcile_loop())

    assert observed == [(False, False)]


def test_normal_engine_without_halt_starts_unhalted(tmp_path):
    eng = Engine(make_cfg(tmp_path))

    assert eng.halted is False
    assert eng._halt_state_error is None
