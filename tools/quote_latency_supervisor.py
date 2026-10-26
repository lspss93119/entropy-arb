#!/usr/bin/env python3
"""Bounded supervisor for repeated, public-feed-only quote captures.

The supervisor invokes the recorder CLI one hour at a time.  It does not
import the trading Engine, load credentials, or invoke a shell.  A config is
validated for the exact SNDK x Lighter RH research pair before any child is
started.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


STOP_REQUESTED = False


def directory_bytes(root: Path) -> int:
    """Return regular-file bytes without following symlinked files."""
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            try:
                total += path.stat().st_size
            except OSError:
                continue
    return total


def build_capture_command(config: dict[str, Any]) -> list[str]:
    """Build one bounded one-hour recorder invocation without a shell."""
    def value(name: str) -> str:
        return str(config[name])

    command = [
        value("python_path"), value("cli_path"),
        "--symbol", "SNDK", "--hedge", "lighter-rh",
        "--output-dir", value("output_dir"),
        "--duration-sec", "3600",
        "--rolling-csv", value("rolling_csv"),
        "--fees-bps", value("fees_bps"),
        "--sell-quantile", value("sell_quantile"),
        "--buy-quantile", value("buy_quantile"),
        "--depth-levels", value("depth_levels"),
        "--pair-max-skew-ms", value("pair_max_skew_ms"),
        "--max-file-rows", value("max_file_rows"),
        "--max-file-bytes", value("max_file_bytes"),
        "--retention-hours", value("retention_hours"),
        "--max-files", value("max_files"),
        "--log-level", "WARNING",
    ]
    if config.get("preserve_raw", False):
        command.append("--preserve-raw")
    return command


def load_config(path: Path) -> dict[str, Any]:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema_version") != 1:
        raise ValueError("unsupported supervisor schema")
    if config.get("market") != "io:SNDK|lighter-rh:SNDK":
        raise ValueError("supervisor is restricted to SNDK x Lighter RH")
    if config.get("duration_sec") != 3600:
        raise ValueError("each capture must use the CLI one-hour limit")
    if int(config.get("target_runs", 0)) <= 0:
        raise ValueError("target_runs must be positive")
    if float(config.get("retention_hours", 0)) < 48:
        raise ValueError("retention_hours must preserve the requested 48 hours")
    if int(config.get("max_data_bytes", 0)) <= 0:
        raise ValueError("max_data_bytes must be positive")
    for key in ("python_path", "cli_path", "output_dir", "rolling_csv"):
        if not config.get(key):
            raise ValueError(f"missing supervisor config: {key}")
    return config


def sanitized_environment() -> dict[str, str]:
    """Pass only benign process settings; never propagate account variables."""
    allowed = {"PATH", "HOME", "LANG", "LC_ALL", "TZ"}
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _signal_handler(signum, _frame) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True


def run_supervisor(config_path: Path) -> int:
    config = load_config(config_path)
    output_dir = Path(config["output_dir"])
    state_path = Path(config["supervisor_manifest"])
    log_path = Path(config["supervisor_log"])
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "record_only": True,
        "market": config["market"],
        "source_commit": config["source_commit"],
        "target_runs": int(config["target_runs"]),
        "completed_runs": 0,
        "failed_runs": 0,
        "consecutive_failures": 0,
        "status": "RUNNING",
        "started_wall_ns": time.time_ns(),
        "current_child_pid": None,
        "last_return_code": None,
        "last_data_bytes": directory_bytes(output_dir),
    }
    _write_state(state_path, state)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    max_failures = int(config.get("max_consecutive_failures", 3))
    backoff_sec = int(config.get("retry_backoff_sec", 300))
    duration_sec = int(config["duration_sec"])
    timeout_sec = duration_sec + int(config.get("timeout_grace_sec", 120))

    try:
        for _slot in range(int(config["target_runs"])):
            if STOP_REQUESTED:
                state["status"] = "STOPPED"
                break
            current_bytes = directory_bytes(output_dir)
            state["last_data_bytes"] = current_bytes
            if current_bytes >= int(config["max_data_bytes"]):
                state["status"] = "HALTED_DISK_CAP"
                break

            command = build_capture_command(config)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps({
                    "event": "run_start", "slot": _slot,
                    "command": command, "wall_ns": time.time_ns(),
                }) + "\n")
                log.flush()
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, env=sanitized_environment(),
                )
                state["current_child_pid"] = process.pid
                _write_state(state_path, state)
                deadline = time.monotonic() + timeout_sec
                while process.poll() is None:
                    if STOP_REQUESTED:
                        process.terminate()
                        break
                    if time.monotonic() >= deadline:
                        process.terminate()
                        break
                    time.sleep(1)
                try:
                    return_code = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    return_code = process.wait(timeout=10)
                log.write(json.dumps({
                    "event": "run_end", "slot": _slot,
                    "return_code": return_code, "wall_ns": time.time_ns(),
                }) + "\n")

            state["current_child_pid"] = None
            state["last_return_code"] = return_code
            state["last_data_bytes"] = directory_bytes(output_dir)
            if STOP_REQUESTED:
                state["status"] = "STOPPED"
                break
            if return_code == 0:
                state["completed_runs"] += 1
                state["consecutive_failures"] = 0
            else:
                state["failed_runs"] += 1
                state["consecutive_failures"] += 1
                if state["consecutive_failures"] >= max_failures:
                    state["status"] = "HALTED_CONSECUTIVE_FAILURES"
                    break
                time.sleep(backoff_sec)
            _write_state(state_path, state)
        else:
            state["status"] = (
                "COMPLETED" if state["completed_runs"] == state["target_runs"]
                else "HALTED_ATTEMPT_LIMIT"
            )
    finally:
        state["current_child_pid"] = None
        state.setdefault("status", "STOPPED")
        state["ended_wall_ns"] = time.time_ns()
        state["last_data_bytes"] = directory_bytes(output_dir)
        _write_state(state_path, state)
    return 0 if state["status"] == "COMPLETED" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="bounded SNDK RH quote recorder supervisor")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    try:
        return run_supervisor(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"supervisor configuration/runtime error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
